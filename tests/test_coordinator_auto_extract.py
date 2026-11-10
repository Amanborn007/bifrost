"""Coordinator wait-and-integrate path — _wait_and_integrate_extractions().

Tests the wait-loop helper extracted from ``run_research`` so the
event/promotion logic can be exercised without spinning up the full
research pipeline. The worker, session factory, and entity-count
query are all stubbed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

# Use real-shaped UUID strings for the fake repo IDs because the
# Entity.scope_repo_id column is typed as UUID(); SQLAlchemy validates
# bind parameters against the column type.
_R_ORDERS = "11111111-1111-1111-1111-111111111111"
_R_AUTH = "22222222-2222-2222-2222-222222222222"
_R_FAST = "33333333-3333-3333-3333-333333333333"
_R_SLOW = "44444444-4444-4444-4444-444444444444"
_R_FAIL = "55555555-5555-5555-5555-555555555555"
_R_EMPTY = "66666666-6666-6666-6666-666666666666"
_R_WEIRD = "77777777-7777-7777-7777-777777777777"


@dataclass
class _FakeRepo:
    id: str
    name: str


class _FakeWorker:
    """Stand-in for ExtractionWorker. ``wait_for_task`` returns the
    pre-programmed final state for each task, optionally after a
    delay."""

    def __init__(self, outcomes: dict[str, tuple[str | None, float]]):
        # outcomes[task_id] = (final_state, delay_seconds)
        self._outcomes = outcomes

    async def wait_for_task(
        self,
        task_id: str,
        *,
        timeout: float,
    ) -> str | None:
        if task_id not in self._outcomes:
            return None
        state, delay = self._outcomes[task_id]
        # Honor caller's timeout so we can exercise the timeout path.
        try:
            await asyncio.wait_for(asyncio.sleep(delay), timeout=timeout)
        except TimeoutError:
            return None
        return state


class _FakeSessionFactory:
    """Returns a fake async session whose ``execute`` returns a result
    with ``.scalar()`` giving the pre-programmed entity count for a
    repo. The count is keyed by repo_id via the count_by_repo dict."""

    def __init__(self, count_by_repo: dict[str, int]):
        self._counts = count_by_repo

    def __call__(self) -> _FakeAsyncSessionCtx:
        return _FakeAsyncSessionCtx(self._counts)


class _FakeAsyncSessionCtx:
    def __init__(self, counts: dict[str, int]):
        self._counts = counts

    async def __aenter__(self) -> _FakeAsyncSession:
        return _FakeAsyncSession(self._counts)

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeAsyncSession:
    def __init__(self, counts: dict[str, int]):
        self._counts = counts

    async def execute(self, stmt) -> _FakeResult:  # noqa: ANN001
        # Walk the rendered SQL with bind params shown as their str()
        # form. SQLAlchemy renders the WHERE-clause repo_id as a bind
        # parameter; we inspect ``stmt.compile().params`` to find the
        # value the helper passed in.
        compiled = stmt.compile()
        params = dict(getattr(compiled, "params", {}) or {})
        match_count = 0
        for v in params.values():
            key = str(v)
            if key in self._counts:
                match_count = self._counts[key]
                break
        return _FakeResult(match_count)


class _FakeResult:
    def __init__(self, value: int):
        self._value = value

    def scalar(self) -> int:
        return self._value


@pytest.mark.asyncio
async def test_all_succeed_with_entities_are_promoted() -> None:
    from bifrost.research.coordinator import _wait_and_integrate_extractions

    repos = [
        ("t-1", _R_ORDERS, _FakeRepo(id=_R_ORDERS, name="orders")),
        ("t-2", _R_AUTH, _FakeRepo(id=_R_AUTH, name="auth")),
    ]
    worker = _FakeWorker({"t-1": ("success", 0.01), "t-2": ("success", 0.02)})
    factory = _FakeSessionFactory({_R_ORDERS: 50, _R_AUTH: 30})

    promoted: list[Any] = []
    promoted_ids: set[str] = set()
    events = []
    async for evt in _wait_and_integrate_extractions(
        worker=worker,
        queued_repos=repos,
        wait_s=2.0,
        session_factory=factory,
        promoted_out=promoted,
        promoted_ids_out=promoted_ids,
    ):
        events.append(evt)

    assert {e["type"] for e in events} == {"extraction_completed_inline"}
    assert len(events) == 2
    assert {r.name for r in promoted} == {"orders", "auth"}
    assert promoted_ids == {_R_ORDERS, _R_AUTH}
    # Each event reports the entity count.
    counts = {e["repo_name"]: e["entities"] for e in events}
    assert counts == {"orders": 50, "auth": 30}


@pytest.mark.asyncio
async def test_success_but_zero_entities_emits_no_entities_event() -> None:
    from bifrost.research.coordinator import _wait_and_integrate_extractions

    repos = [("t-1", _R_EMPTY, _FakeRepo(id=_R_EMPTY, name="empty-repo"))]
    worker = _FakeWorker({"t-1": ("success", 0.01)})
    factory = _FakeSessionFactory({_R_EMPTY: 0})

    promoted: list[Any] = []
    promoted_ids: set[str] = set()
    events = []
    async for evt in _wait_and_integrate_extractions(
        worker=worker,
        queued_repos=repos,
        wait_s=2.0,
        session_factory=factory,
        promoted_out=promoted,
        promoted_ids_out=promoted_ids,
    ):
        events.append(evt)

    assert events[0]["type"] == "extraction_no_entities"
    assert promoted == []
    assert promoted_ids == set()


@pytest.mark.asyncio
async def test_failed_extraction_emits_failed_event() -> None:
    from bifrost.research.coordinator import _wait_and_integrate_extractions

    repos = [("t-1", _R_FAIL, _FakeRepo(id=_R_FAIL, name="fail-repo"))]
    worker = _FakeWorker({"t-1": ("failed", 0.01)})
    factory = _FakeSessionFactory({_R_FAIL: 0})

    promoted: list[Any] = []
    events = []
    async for evt in _wait_and_integrate_extractions(
        worker=worker,
        queued_repos=repos,
        wait_s=2.0,
        session_factory=factory,
        promoted_out=promoted,
        promoted_ids_out=set(),
    ):
        events.append(evt)

    assert events[0]["type"] == "extraction_failed_inline"
    assert events[0]["state"] == "failed"
    assert promoted == []


@pytest.mark.asyncio
async def test_timeout_emits_extraction_timeout_event() -> None:
    from bifrost.research.coordinator import _wait_and_integrate_extractions

    repos = [("t-slow", _R_SLOW, _FakeRepo(id=_R_SLOW, name="slow"))]
    # Worker reports completion AFTER 5s, but our budget is 0.1s.
    worker = _FakeWorker({"t-slow": ("success", 5.0)})
    factory = _FakeSessionFactory({_R_SLOW: 99})

    promoted: list[Any] = []
    events = []
    async for evt in _wait_and_integrate_extractions(
        worker=worker,
        queued_repos=repos,
        wait_s=0.1,
        session_factory=factory,
        promoted_out=promoted,
        promoted_ids_out=set(),
    ):
        events.append(evt)

    assert any(e["type"] == "extraction_timeout" for e in events)
    assert promoted == []


@pytest.mark.asyncio
async def test_mixed_outcomes_partial_promotion() -> None:
    from bifrost.research.coordinator import _wait_and_integrate_extractions

    repos = [
        ("t-fast", _R_FAST, _FakeRepo(id=_R_FAST, name="fast")),
        ("t-slow", _R_SLOW, _FakeRepo(id=_R_SLOW, name="slow")),
    ]
    worker = _FakeWorker(
        {
            "t-fast": ("success", 0.01),
            "t-slow": ("success", 5.0),
        }
    )
    factory = _FakeSessionFactory({_R_FAST: 12, _R_SLOW: 99})

    promoted: list[Any] = []
    promoted_ids: set[str] = set()
    events = []
    async for evt in _wait_and_integrate_extractions(
        worker=worker,
        queued_repos=repos,
        wait_s=0.3,
        session_factory=factory,
        promoted_out=promoted,
        promoted_ids_out=promoted_ids,
    ):
        events.append(evt)

    types = [e["type"] for e in events]
    assert "extraction_completed_inline" in types
    assert "extraction_timeout" in types
    assert promoted == [repos[0][2]]  # only fast was promoted
    assert promoted_ids == {_R_FAST}


@pytest.mark.asyncio
async def test_unknown_state_emits_failed_with_unknown() -> None:
    """Worker returns None (race / not tracked) — treated as unknown."""
    from bifrost.research.coordinator import _wait_and_integrate_extractions

    repos = [("t-?", _R_WEIRD, _FakeRepo(id=_R_WEIRD, name="weird"))]
    worker = _FakeWorker({"t-?": (None, 0.01)})
    factory = _FakeSessionFactory({_R_WEIRD: 0})

    events = []
    async for evt in _wait_and_integrate_extractions(
        worker=worker,
        queued_repos=repos,
        wait_s=2.0,
        session_factory=factory,
        promoted_out=[],
        promoted_ids_out=set(),
    ):
        events.append(evt)

    assert events[0]["type"] == "extraction_failed_inline"
    assert events[0]["state"] == "unknown"


@pytest.mark.asyncio
async def test_empty_queue_yields_nothing() -> None:
    from bifrost.research.coordinator import _wait_and_integrate_extractions

    events = []
    async for evt in _wait_and_integrate_extractions(
        worker=_FakeWorker({}),
        queued_repos=[],
        wait_s=1.0,
        session_factory=_FakeSessionFactory({}),
        promoted_out=[],
        promoted_ids_out=set(),
    ):
        events.append(evt)

    assert events == []
