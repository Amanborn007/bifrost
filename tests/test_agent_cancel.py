"""Per-agent cancel registry + tracked-task wrapper.

Tests for the in-process registry helpers in
``bifrost.research.coordinator``:

  * ``_register_agent_task`` / ``_unregister_agent_task`` round-trip
  * ``cancel_agent_task`` returns False for missing / done tasks,
    True for running tasks
  * ``_tracked_external_agent`` registers, unregisters in finally,
    and on user-initiated cancel returns the synthetic
    ``cancelled_report`` instead of raising — so the bundle's other
    agents can keep running.

No DB, no API, no Claude CLI — pure asyncio behaviour around the
registry + wrapper.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from bifrost.research.coordinator import (
    _AGENT_TASKS,
    _register_agent_task,
    _tracked_external_agent,
    _unregister_agent_task,
    cancel_agent_task,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts from an empty registry — the in-process
    dict is module-global so a leaked entry would corrupt the next
    test's expectations."""
    _AGENT_TASKS.clear()
    yield
    _AGENT_TASKS.clear()


# ── Registry round-trip ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_and_unregister() -> None:
    async def _noop() -> str:
        return "done"

    task = asyncio.create_task(_noop())
    _register_agent_task("conv-1", "repo-a", "jira", task)
    assert ("conv-1", "repo-a", "jira") in _AGENT_TASKS
    await task
    _unregister_agent_task("conv-1", "repo-a", "jira")
    assert ("conv-1", "repo-a", "jira") not in _AGENT_TASKS


@pytest.mark.asyncio
async def test_unregister_is_idempotent() -> None:
    """Calling unregister on a missing key shouldn't raise — the
    finally block runs even if registration failed."""
    _unregister_agent_task("never", "registered", "nope")  # no error


# ── cancel_agent_task ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_returns_false_for_missing_task() -> None:
    assert cancel_agent_task("conv-1", "repo-a", "jira") is False


@pytest.mark.asyncio
async def test_cancel_returns_false_for_done_task() -> None:
    async def _quick() -> int:
        return 42

    task = asyncio.create_task(_quick())
    _register_agent_task("conv-1", "repo-a", "jira", task)
    await task
    # Task is done — cancel should report "not running".
    assert cancel_agent_task("conv-1", "repo-a", "jira") is False


@pytest.mark.asyncio
async def test_cancel_returns_true_for_running_task() -> None:
    async def _slow() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise

    task = asyncio.create_task(_slow())
    _register_agent_task("conv-1", "repo-a", "jira", task)
    # Yield once so the task starts.
    await asyncio.sleep(0)
    assert cancel_agent_task("conv-1", "repo-a", "jira") is True
    # Wait for the task to finish processing the cancel.
    with pytest.raises(asyncio.CancelledError):
        await task


# ── _tracked_external_agent ─────────────────────────────────────


@dataclass
class _FakeReport:
    """Stand-in for JiraReport/ConfluenceReport/etc — only the
    ``error`` field matters for the cancel-path tests."""

    relevance: str
    summary: str
    error: str | None = None


@pytest.mark.asyncio
async def test_tracked_agent_registers_and_clears_on_success() -> None:
    """Happy path: the wrapper registers the task, awaits the inner
    coroutine, returns its result, and clears the registry."""

    async def _inner() -> _FakeReport:
        return _FakeReport(relevance="medium", summary="ok")

    result = await _tracked_external_agent(
        conv_id="conv-1",
        repo_id="repo-a",
        source="jira",
        coro=_inner(),
        cancelled_report=_FakeReport(
            relevance="none",
            summary="(cancelled)",
            error="cancelled by user",
        ),
    )
    assert result.relevance == "medium"
    assert ("conv-1", "repo-a", "jira") not in _AGENT_TASKS


@pytest.mark.asyncio
async def test_tracked_agent_returns_cancelled_report_on_cancel() -> None:
    """Cancellation contract: when ``cancel_agent_task`` fires, the
    wrapper swallows ``CancelledError`` and returns the synthetic
    ``cancelled_report`` so the bundle's other agents keep running.
    """

    async def _slow() -> _FakeReport:
        await asyncio.sleep(10)
        return _FakeReport(relevance="high", summary="never reached")

    cancelled = _FakeReport(
        relevance="none",
        summary="(cancelled by user)",
        error="cancelled by user",
    )
    task = asyncio.create_task(
        _tracked_external_agent(
            conv_id="conv-1",
            repo_id="repo-a",
            source="confluence",
            coro=_slow(),
            cancelled_report=cancelled,
        ),
    )
    # Yield so the wrapper registers the task.
    await asyncio.sleep(0)
    assert cancel_agent_task("conv-1", "repo-a", "confluence") is True
    result = await task
    assert result is cancelled
    assert result.error == "cancelled by user"
    # Registry was cleared in finally.
    assert ("conv-1", "repo-a", "confluence") not in _AGENT_TASKS


@pytest.mark.asyncio
async def test_tracked_agent_does_not_swallow_other_exceptions() -> None:
    """Only ``CancelledError`` becomes a synthetic report. A normal
    exception inside the inner coroutine propagates so the bundle
    can record a real error in the report's ``error`` field —
    different code path from cancellation."""

    class _Boom(Exception):
        pass

    async def _boom() -> None:
        raise _Boom("explode")

    with pytest.raises(_Boom):
        await _tracked_external_agent(
            conv_id="conv-1",
            repo_id="repo-a",
            source="github",
            coro=_boom(),
            cancelled_report=_FakeReport(
                relevance="none",
                summary="x",
                error="cancelled",
            ),
        )
    # Even on exception, finally clears the registry.
    assert ("conv-1", "repo-a", "github") not in _AGENT_TASKS


@pytest.mark.asyncio
async def test_tracked_agent_isolates_concurrent_cancels() -> None:
    """Cancelling one agent for one repo MUST NOT cancel a different
    agent for the same repo or the same agent for a different repo.
    The (conv, repo, source) tuple is the cancel granularity."""
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def _hold_until(_n: str) -> _FakeReport:
        started.set()
        try:
            await asyncio.wait_for(proceed.wait(), timeout=10)
        except asyncio.CancelledError:
            raise
        return _FakeReport(relevance="high", summary=f"finished {_n}")

    cancelled_a = _FakeReport(relevance="none", summary="cancelled-a", error="cancelled by user")
    cancelled_b = _FakeReport(relevance="none", summary="cancelled-b", error="cancelled by user")

    task_a = asyncio.create_task(
        _tracked_external_agent(
            conv_id="conv-1",
            repo_id="repo-a",
            source="jira",
            coro=_hold_until("a"),
            cancelled_report=cancelled_a,
        ),
    )
    task_b = asyncio.create_task(
        _tracked_external_agent(
            conv_id="conv-1",
            repo_id="repo-a",
            source="confluence",
            coro=_hold_until("b"),
            cancelled_report=cancelled_b,
        ),
    )
    # Wait until both registered.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Cancel ONLY jira.
    assert cancel_agent_task("conv-1", "repo-a", "jira") is True

    # Let confluence finish.
    proceed.set()

    res_a = await task_a
    res_b = await task_b
    assert res_a is cancelled_a  # cancelled
    assert res_b.summary == "finished b"  # untouched
