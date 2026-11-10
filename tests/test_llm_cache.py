"""Tests for the content-addressed LLM response cache (cost control)."""

from __future__ import annotations

import asyncio

import pytest

from bifrost.extraction.cli_client import (
    ClaudeLlmClient,
    CliCompletion,
    Usage,
    _completion_is_cacheable,
    _is_cacheable_text,
)
from bifrost.extraction.llm_cache import make_cache_key


def _completion(parsed: dict, text: str = "") -> CliCompletion:
    return CliCompletion(parsed=parsed, usage=Usage(0, 0, 0, 0, 0.0), text=text)


def test_make_cache_key_stable_and_distinct() -> None:
    base = dict(system_prompt="sys", user_message="hi", model="m", max_turns=1)
    k1 = make_cache_key(**base, json_schema={"type": "object"})
    k2 = make_cache_key(**base, json_schema={"type": "object"})
    assert k1 == k2 and k1.startswith("llmcache:")
    # Any input change → different key.
    assert make_cache_key(**{**base, "user_message": "bye"}, json_schema={"type": "object"}) != k1
    assert make_cache_key(**{**base, "model": "other"}, json_schema={"type": "object"}) != k1
    assert make_cache_key(**base, json_schema={"type": "string"}) != k1
    assert make_cache_key(**base, json_schema={"type": "object"}, allowed_tools=["Read"]) != k1


class _StubCache:
    """In-memory stand-in for LlmResponseCache (no DB)."""

    def __init__(self, hit: dict | None = None) -> None:
        self._hit = hit
        self.put_calls: list[tuple[str, dict]] = []
        self.get_keys: list[str] = []

    async def get(self, key: str) -> dict | None:
        self.get_keys.append(key)
        return self._hit

    async def put(self, key: str, payload: dict) -> None:
        self.put_calls.append((key, payload))

    async def record_saving(self, *, model: str, saved_usd: float) -> None:
        self.savings: list[tuple[str, float]] = getattr(self, "savings", [])
        self.savings.append((model, saved_usd))


def _boom(*_a, **_kw):  # stand-in for create_subprocess_exec — must NOT be reached
    raise RuntimeError("subprocess spawned — cache did not short-circuit")


@pytest.mark.asyncio
async def test_cache_hit_short_circuits_without_spawning(monkeypatch) -> None:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    client = ClaudeLlmClient()
    client._cache = _StubCache(hit={"parsed": {"answer": "cached"}, "text": "hello"})  # noqa: SLF001

    out = await client.call_with_json_schema(
        system_prompt="s",
        user_message="u",
        model="m",
        json_schema={"type": "object"},
    )
    assert isinstance(out, CliCompletion)
    assert out.parsed == {"answer": "cached"}
    assert out.text == "hello"
    # A hit costs nothing — usage zeroed so cost logging shows the saving.
    assert out.usage.total_cost_usd == 0.0


@pytest.mark.asyncio
async def test_force_refresh_bypasses_cache(monkeypatch) -> None:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    client = ClaudeLlmClient()
    client._cache = _StubCache(hit={"parsed": {"x": 1}, "text": ""})  # noqa: SLF001

    # force_refresh must skip the cache and proceed to spawn (our _boom).
    with pytest.raises(RuntimeError, match="subprocess spawned"):
        await client.call_with_json_schema(
            system_prompt="s",
            user_message="u",
            model="m",
            json_schema={"type": "object"},
            force_refresh=True,
        )


@pytest.mark.asyncio
async def test_cache_key_override_is_used(monkeypatch) -> None:
    # Extraction passes a canary-stripped stable key so re-runs hit despite the
    # per-call random canary in the prompt. Verify the override key is what the
    # cache is queried with (not the prompt hash).
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    client = ClaudeLlmClient()
    stub = _StubCache(hit={"parsed": {"ok": 1}, "text": ""})
    client._cache = stub  # noqa: SLF001

    out = await client.call_with_json_schema(
        system_prompt="s",
        user_message="content + canary-RANDOM-abc123",  # would-be-unique prompt
        model="m",
        json_schema={"type": "object"},
        cache_key_override="STABLE-CONTENT-KEY",
    )
    assert out.parsed == {"ok": 1}
    assert stub.get_keys == ["STABLE-CONTENT-KEY"]


@pytest.mark.asyncio
async def test_no_cache_when_disabled() -> None:
    from bifrost.cli.config import BifrostSettings

    client = ClaudeLlmClient(BifrostSettings(llm_cache_enabled=False))
    assert client._cache is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_stream_text_cache_hit_replays_without_spawning(monkeypatch) -> None:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    client = ClaudeLlmClient()
    client._cache = _StubCache(hit={"text": "the cached answer"})  # noqa: SLF001

    events = [
        evt async for evt in client.stream_text(system_prompt="s", user_message="u", model="m")
    ]
    kinds = [e["type"] for e in events]
    assert "delta" in kinds and kinds[-1] == "done"
    full = "".join(e["text"] for e in events if e["type"] == "delta")
    assert full == "the cached answer"
    assert events[-1]["usage"].total_cost_usd == 0.0


# ── Cache-poisoning guard ───────────────────────────────────────────────
# A bad result (empty / error / "nothing found" / all-none relevance) must
# NEVER be cached — otherwise every identical retry replays it for $0 and the
# user can't recover by re-running.


def test_completion_cacheable_with_real_payload() -> None:
    assert _completion_is_cacheable(_completion({"relevance": "high", "key_components": ["Foo"]}))
    assert _completion_is_cacheable(_completion({}, text="a real answer body"))


def test_completion_not_cacheable_when_empty() -> None:
    assert not _completion_is_cacheable(_completion({}, text=""))
    assert not _completion_is_cacheable(_completion({}, text="   "))


def test_completion_not_cacheable_with_error_field() -> None:
    assert not _completion_is_cacheable(
        _completion({"relevance": "low", "error": "MCP search failed"})
    )


def test_completion_not_cacheable_when_relevance_none_no_payload() -> None:
    # "looked, found nothing" — caching this would block a real answer on retry.
    assert not _completion_is_cacheable(_completion({"relevance": "none"}))
    assert not _completion_is_cacheable(
        _completion({"relevance": "none", "rankings": [], "summary": ""})
    )


def test_completion_cacheable_when_relevance_none_but_has_payload() -> None:
    # A none-relevance report that still carries findings is a real result.
    assert _completion_is_cacheable(
        _completion({"relevance": "none", "summary": "Looked at X, here is why"})
    )


def test_text_cacheable_threshold_and_failure_markers() -> None:
    assert _is_cacheable_text("x" * 200)
    # Too short → degraded stub, don't cache.
    assert not _is_cacheable_text("done")
    assert not _is_cacheable_text("")
    # Self-reported failure → don't cache even if long.
    assert not _is_cacheable_text(
        "Nothing useful was found for this question after a thorough search."
    )
    assert not _is_cacheable_text(
        "I couldn't complete this research within the available budget here."
    )


@pytest.mark.asyncio
async def test_store_completion_skips_poisoned_result() -> None:
    client = ClaudeLlmClient()
    stub = _StubCache()
    client._cache = stub  # noqa: SLF001

    # Error report → not stored.
    await client._store_completion(  # noqa: SLF001
        "k1", _completion({"relevance": "none", "error": "boom"}), "m"
    )
    # Empty → not stored.
    await client._store_completion("k2", _completion({}, text=""), "m")  # noqa: SLF001
    assert stub.put_calls == []

    # Real result → stored.
    await client._store_completion(  # noqa: SLF001
        "k3", _completion({"relevance": "high", "answer": "yes"}), "m"
    )
    assert [k for k, _ in stub.put_calls] == ["k3"]


@pytest.mark.asyncio
async def test_stream_text_does_not_cache_degraded_stub(monkeypatch) -> None:
    # A short/failure final answer must not be put() into the cache.
    client = ClaudeLlmClient()
    stub = _StubCache()
    client._cache = stub  # noqa: SLF001

    class _FakeProc:
        returncode = 0

        def __init__(self) -> None:
            self.stdin = None
            self.stderr = _EmptyReader()
            self.stdout = _LineReader(
                [
                    b'{"type":"result","result":"Nothing found.","total_cost_usd":0.0}\n',
                ]
            )

        async def wait(self) -> int:
            return 0

    async def _fake_exec(*_a, **_kw):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    events = [e async for e in client.stream_text(system_prompt="s", user_message="u", model="m")]
    assert events[-1]["type"] == "done"
    # "Nothing found." is short + a failure marker → not cached.
    assert stub.put_calls == []


class _LineReader:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _EmptyReader:
    async def read(self) -> bytes:
        return b""


@pytest.mark.asyncio
async def test_stream_text_not_cached_when_tools_present(monkeypatch) -> None:
    # Tool/dir calls explore the live repo → must NOT be served from the
    # prompt-hash cache (would be stale); they fall through to a live spawn.
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    client = ClaudeLlmClient()
    client._cache = _StubCache(hit={"text": "stale"})  # noqa: SLF001

    with pytest.raises(RuntimeError, match="subprocess spawned"):
        async for _ in client.stream_text(
            system_prompt="s",
            user_message="u",
            model="m",
            allowed_tools=["Read"],
            add_dirs=["/tmp/repo"],
        ):
            pass
