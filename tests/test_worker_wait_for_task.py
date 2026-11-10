"""Worker.wait_for_task() — completion-event hook foundation.

The chat coordinator uses ``wait_for_task`` to block on
first-touch auto-extractions before falling back to manifest-only data.
These tests exercise the event registry without spinning up a real
extraction pipeline.
"""

from __future__ import annotations

import asyncio

import pytest


class _DummyToken:
    """Minimal stand-in for CancellationToken used by _RunningTask."""

    def request_cancel(self) -> None:  # pragma: no cover - unused here
        pass


class _DummyCheckpoint:  # pragma: no cover - unused here
    pass


def _make_worker():
    """Construct a worker without connecting to Postgres or filesystem."""
    from bifrost.extraction.worker import ExtractionWorker

    worker = ExtractionWorker.__new__(ExtractionWorker)
    worker._session_factory = None  # type: ignore[attr-defined]
    worker._settings = None  # type: ignore[attr-defined]
    worker._semaphore = asyncio.Semaphore(1)
    worker._running = {}
    worker._heartbeat_task = None
    worker._completion = {}
    worker._final_state = {}
    worker._completion_max = 8
    return worker


@pytest.mark.asyncio
async def test_returns_immediately_when_already_done() -> None:
    """If a prior run already recorded a final state and the task is no
    longer running, return that state without waiting."""
    worker = _make_worker()
    worker._final_state["abc"] = "success"

    out = await worker.wait_for_task("abc", timeout=0.01)

    assert out == "success"


@pytest.mark.asyncio
async def test_returns_none_on_timeout_when_event_unset() -> None:
    """Pending event + timeout → ``None`` (caller treats as no answer)."""
    worker = _make_worker()
    worker._completion["task-x"] = asyncio.Event()  # never set

    out = await worker.wait_for_task("task-x", timeout=0.05)

    assert out is None


@pytest.mark.asyncio
async def test_wakes_up_when_event_set() -> None:
    """A waiter is unblocked the moment the worker sets the event."""
    worker = _make_worker()
    ev = asyncio.Event()
    worker._completion["task-y"] = ev

    async def _set_after():
        await asyncio.sleep(0.02)
        worker._final_state["task-y"] = "failed"
        ev.set()

    async def _waiter():
        return await worker.wait_for_task("task-y", timeout=2.0)

    waiter, _ = await asyncio.gather(_waiter(), _set_after())

    assert waiter == "failed"


@pytest.mark.asyncio
async def test_unknown_task_returns_none() -> None:
    """A task ID that was never submitted yields ``None`` quickly."""
    worker = _make_worker()

    out = await worker.wait_for_task("never-submitted", timeout=0.01)

    assert out is None


def test_resubmit_replaces_old_event() -> None:
    """A re-submitted task ID resets the completion record so a stale
    'set' event from the prior run doesn't immediately satisfy a new
    waiter."""
    worker = _make_worker()

    # Simulate a prior run completing.
    worker._final_state["task-z"] = "success"
    old = asyncio.Event()
    old.set()
    worker._completion["task-z"] = old

    worker._reset_completion_record("task-z")

    new = worker._completion["task-z"]
    assert new is not old
    assert not new.is_set()
    # And the prior final-state was cleared.
    assert "task-z" not in worker._final_state


def test_eviction_bounds_registry_size() -> None:
    """Once the cap is exceeded, oldest entries get dropped — but the
    task currently being reset is preserved."""
    worker = _make_worker()
    worker._completion_max = 4

    for tid in ("a", "b", "c", "d"):
        worker._completion[tid] = asyncio.Event()
        worker._final_state[tid] = "success"

    worker._reset_completion_record("e")  # 5th — triggers eviction

    assert "e" in worker._completion
    # At least one of the older ones got evicted.
    assert len(worker._completion) <= 5
    assert "a" not in worker._completion or "b" not in worker._completion
