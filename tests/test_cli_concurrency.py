"""Tests for the process-wide `claude -p` concurrency limiter and the
init-then-exit-1 transient classification (cli_client.py).

Context: the chat fan-out (6 research agents) PLUS the extraction worker (each
extraction spawns ~6 `claude -p` internally) share ONE Claude OAuth session in
the same backend event loop. 20-40 concurrent spawns oversubscribe the session
and the CLI exits 1 right after its `init` event. A module-level semaphore
bounds the spawn count; the init-then-exit-1 failure is classified transient so
callers retry.

Hermetic: no real `claude` binary — `asyncio.create_subprocess_exec` is
monkeypatched (mirrors tests/test_llm_cache.py).
"""

from __future__ import annotations

import asyncio

import pytest

from bifrost.cli.config import BifrostSettings
from bifrost.extraction import cli_client
from bifrost.extraction.cli_client import (
    ClaudeCliTransientError,
    ClaudeLlmClient,
)


def _settings(limit: int) -> BifrostSettings:
    # Cache off so calls always proceed to the (mocked) spawn path.
    return BifrostSettings(
        llm_cache_enabled=False,
        max_concurrent_cli_calls=limit,
    )


@pytest.fixture(autouse=True)
def _reset_module_semaphore():
    """Each test gets a fresh module-level semaphore (the limiter is rebuilt
    when the configured limit or event loop changes, but reset explicitly so
    tests never leak in-flight slots into one another)."""
    cli_client._CLI_SEMAPHORE = None
    cli_client._CLI_SEMAPHORE_LIMIT = None
    cli_client._CLI_SEMAPHORE_LOOP = None
    yield
    cli_client._CLI_SEMAPHORE = None
    cli_client._CLI_SEMAPHORE_LIMIT = None
    cli_client._CLI_SEMAPHORE_LOOP = None


class _SlowProc:
    """Fake subprocess whose communicate() blocks briefly so concurrent callers
    overlap — letting us measure peak in-flight spawn count."""

    returncode = 0

    def __init__(self, tracker: _ConcurrencyTracker, hold: float) -> None:
        self._tracker = tracker
        self._hold = hold

    async def communicate(self, _stdin: bytes | None = None) -> tuple[bytes, bytes]:
        # A valid `--output-format json` stream with a StructuredOutput block.
        self._tracker.enter()
        try:
            await asyncio.sleep(self._hold)
        finally:
            self._tracker.exit()
        stdout = (
            b'[{"type":"assistant","message":{"content":['
            b'{"type":"tool_use","name":"StructuredOutput","input":{"ok":1}}'
            b"]}},"
            b'{"type":"result","is_error":false,"subtype":"success",'
            b'"result":"done","total_cost_usd":0.0,"usage":{}}]'
        )
        return stdout, b""

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:  # pragma: no cover - not exercised on the happy path
        pass


class _ConcurrencyTracker:
    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0

    def enter(self) -> None:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)

    def exit(self) -> None:
        self.in_flight -= 1


@pytest.mark.asyncio
async def test_semaphore_bounds_concurrency(monkeypatch) -> None:
    """Fire N > limit concurrent call_with_json_schema; peak simultaneous
    in-flight spawns must never exceed the configured limit."""
    limit = 2
    n_calls = 8
    tracker = _ConcurrencyTracker()

    async def _fake_exec(*_a, **_kw):
        # The spawn itself counts as in-flight only once communicate() runs,
        # but the semaphore is acquired BEFORE the spawn — so to measure the
        # spawn ceiling we count inside communicate (held across the slot).
        return _SlowProc(tracker, hold=0.05)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    client = ClaudeLlmClient(_settings(limit))

    async def _one(i: int):
        return await client.call_with_json_schema(
            system_prompt="s",
            user_message=f"u{i}",
            model="m",
            json_schema={"type": "object"},
        )

    results = await asyncio.gather(*[_one(i) for i in range(n_calls)])

    assert len(results) == n_calls
    assert all(r.parsed == {"ok": 1} for r in results)
    # The core assertion: the limiter never let more than `limit` spawns run
    # concurrently, even though 8 were launched at once.
    assert tracker.peak <= limit, f"peak in-flight {tracker.peak} exceeded limit {limit}"
    # And it actually saturated the limit (sanity: the test would pass trivially
    # if everything ran serially due to a bug elsewhere).
    assert tracker.peak == limit


class _InitThenExitProc:
    """Fake subprocess reproducing the OAuth-oversubscription failure: the CLI
    emits only a `system/init` event then exits 1, with empty stderr."""

    returncode = 1

    async def communicate(self, _stdin: bytes | None = None) -> tuple[bytes, bytes]:
        stdout = (
            b'[{"type":"system","subtype":"init","session_id":"abc",'
            b'"model":"claude-sonnet-4-6","tools":[]}]'
        )
        return stdout, b""  # empty stderr — the signal lives only in stdout

    async def wait(self) -> int:
        return 1

    def kill(self) -> None:  # pragma: no cover
        pass


@pytest.mark.asyncio
async def test_init_then_exit_1_raises_transient(monkeypatch) -> None:
    """exit 1 with only an init event (no parseable result, empty stderr) must
    raise ClaudeCliTransientError so the retry wrappers re-attempt it."""

    async def _fake_exec(*_a, **_kw):
        return _InitThenExitProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    client = ClaudeLlmClient(_settings(4))

    with pytest.raises(ClaudeCliTransientError) as ei:
        await client.call_with_json_schema(
            system_prompt="s",
            user_message="u",
            model="m",
            json_schema={"type": "object"},
        )
    # It's marked retryable so the API/orchestration retry path engages.
    assert ei.value.retryable is True
    assert "exit 1" in str(ei.value)


class _MaxTurnsSalvageProc:
    """exit 1 BUT stdout carries a salvageable StructuredOutput result
    (error_max_turns graceful degradation). Must be parsed, NOT raised."""

    returncode = 1

    async def communicate(self, _stdin: bytes | None = None) -> tuple[bytes, bytes]:
        stdout = (
            b'[{"type":"assistant","message":{"content":['
            b'{"type":"tool_use","name":"StructuredOutput","input":{"salvaged":1}}'
            b"]}},"
            b'{"type":"result","is_error":true,"subtype":"error_max_turns",'
            b'"result":"","total_cost_usd":0.0,"usage":{}}]'
        )
        return stdout, b""

    async def wait(self) -> int:
        return 1

    def kill(self) -> None:  # pragma: no cover
        pass


@pytest.mark.asyncio
async def test_exit_1_with_parseable_result_is_salvaged(monkeypatch) -> None:
    """Regression guard: the transient_default change must NOT break the
    error_max_turns salvage path — a result present in stdout is still parsed."""

    async def _fake_exec(*_a, **_kw):
        return _MaxTurnsSalvageProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    client = ClaudeLlmClient(_settings(4))
    out = await client.call_with_json_schema(
        system_prompt="s",
        user_message="u",
        model="m",
        json_schema={"type": "object"},
    )
    assert out.parsed == {"salvaged": 1}
