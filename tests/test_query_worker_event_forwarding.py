"""Query worker → SSE event_bus event-forwarding contract.

Regression test: an explicit allowlist of event types forwarded to the
SSE bus would silently drop the chat-experience events (research_plan,
bundle_reused, prior_subgraph_hydrated, scope_narrowed,
extraction_*) — they leave the coordinator but never reach the UI.

The forwarding passes every non-internal event verbatim. This
test pins that contract so a future refactor doesn't silently
re-introduce an allowlist that misses a new event type.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _evt(etype: str, **payload: Any) -> dict[str, Any]:
    return {"type": etype, **payload}


# Stand-in for run_research that yields a controlled event sequence.
def _scripted(events: list[dict[str, Any]]):
    async def _gen(**_kw: Any) -> AsyncIterator[dict[str, Any]]:
        for e in events:
            yield e

    return _gen


@pytest.fixture
def captured_publishes(monkeypatch):
    """Capture every event sent to ``event_bus.publish``."""
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def _fake_publish(task_id: str, event):  # noqa: ANN001
        # SSEEvent has .event_type and .data attributes.
        calls.append((task_id, event.event_type, dict(event.data)))

    monkeypatch.setattr(
        "bifrost.api.sse.event_bus.publish",
        _fake_publish,
    )
    return calls


@pytest.fixture
def stubbed_repository(monkeypatch):
    """No-op the DB writes that ``run_query_turn`` does after coordinator
    completes — we're testing forwarding, not persistence."""
    fake_repo = MagicMock()
    fake_repo.append_message = AsyncMock(return_value=MagicMock(id="msg-id"))
    fake_repo.touch_conversation = AsyncMock()

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.commit = AsyncMock()

    fake_factory = MagicMock(return_value=fake_session)

    monkeypatch.setattr(
        "bifrost.storage.repository.EntityRepository",
        lambda s: fake_repo,
    )
    return fake_factory, fake_repo


@pytest.mark.asyncio
async def test_forwards_chat_experience_event_types(
    captured_publishes,
    stubbed_repository,
    monkeypatch,
) -> None:
    """All the chat-experience events must reach
    the SSE bus, not be silently dropped."""
    factory, _repo = stubbed_repository

    events = [
        _evt("status", message="Resolving scope…"),
        _evt("scope", candidates=[]),
        _evt("research_plan", mode="extend_bundle", reason="high cosine"),
        _evt("bundle_reused", conversation_id="c1", prior_repos=["orders"], message="x"),
        _evt("scope_narrowed", from_count=5, to_count=1, message="y"),
        _evt("auto_extracting", message="extracting", repos=[], wait_seconds=90),
        _evt(
            "extraction_completed_inline",
            repo_id="r1",
            repo_name="orders",
            task_id="t1",
            entities=12,
            message="ok",
        ),
        _evt(
            "extraction_no_entities", repo_id="r2", repo_name="empty", task_id="t2", message="nope"
        ),
        _evt(
            "extraction_failed_inline",
            repo_id="r3",
            repo_name="bad",
            task_id="t3",
            state="failed",
            message="bad",
        ),
        _evt(
            "extraction_timeout",
            repo_id="r4",
            repo_name="slow",
            task_id="t4",
            wait_seconds=90,
            message="late",
        ),
        _evt("research_upgraded", promoted=[], message="up"),
        _evt(
            "prior_subgraph_hydrated",
            total_entities=12,
            total_edges=23,
            repo_count=2,
            repo_names=["a", "b"],
            missing_urns=0,
        ),
        _evt("synthesis_delta", text="answer "),
        _evt("synthesis_delta", text="text"),
        _evt(
            "done", answer="answer text", sources=[], usage={}, confidence="confirmed", reports=[]
        ),
    ]

    monkeypatch.setattr(
        "bifrost.research.coordinator.run_research",
        _scripted(events),
    )

    import uuid

    from bifrost.extraction.query_worker import QueryWorkerDeps, run_query_turn

    deps = QueryWorkerDeps(
        session_factory=factory,
        settings=MagicMock(),
    )

    # The conversation needs to exist; stub it via the repo mock.
    fake_repo = MagicMock()
    fake_repo.append_message = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))

    async def _fake_get_conversation(_cid):
        return MagicMock(id=uuid.uuid4(), scope_repo_ids=[])

    fake_repo.get_conversation = AsyncMock(side_effect=_fake_get_conversation)
    monkeypatch.setattr(
        "bifrost.storage.repository.EntityRepository",
        lambda s: fake_repo,
    )

    cid = uuid.uuid4()
    try:
        await run_query_turn(
            conversation_id=cid,
            user_message_content="q",
            deps=deps,
        )
    except Exception:
        # The persistence path will fail with our stubs — that's fine,
        # we just care about what was published before that point.
        pass

    forwarded_types = {evt_type for _, evt_type, _ in captured_publishes}

    # Every chat-experience event type must have been forwarded.
    expected_chat_events = {
        "research_plan",
        "bundle_reused",
        "scope_narrowed",
        "auto_extracting",
        "extraction_completed_inline",
        "extraction_no_entities",
        "extraction_failed_inline",
        "extraction_timeout",
        "research_upgraded",
        "prior_subgraph_hydrated",
    }
    missing = expected_chat_events - forwarded_types
    assert not missing, f"forwarder dropped events: {missing}"


@pytest.mark.asyncio
async def test_does_not_forward_internal_underscore_sentinels(
    captured_publishes,
    stubbed_repository,
    monkeypatch,
) -> None:
    """Coordinator-internal sentinels (e.g. ``_clarify_fallback``)
    MUST NOT be forwarded to the SSE bus — they exist only for the
    coordinator's own state machine."""
    factory, _repo = stubbed_repository

    events = [
        _evt("status", message="x"),
        _evt("_clarify_fallback"),  # internal — must drop
        _evt("research_plan", mode="new_scope", reason="x"),
        _evt("done", answer="ok", sources=[], usage={}, confidence="uncertain", reports=[]),
    ]
    monkeypatch.setattr(
        "bifrost.research.coordinator.run_research",
        _scripted(events),
    )

    fake_repo = MagicMock()
    import uuid

    fake_repo.append_message = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))

    async def _fake_get_conversation(_cid):
        return MagicMock(id=uuid.uuid4(), scope_repo_ids=[])

    fake_repo.get_conversation = AsyncMock(side_effect=_fake_get_conversation)
    monkeypatch.setattr(
        "bifrost.storage.repository.EntityRepository",
        lambda s: fake_repo,
    )

    from bifrost.extraction.query_worker import QueryWorkerDeps, run_query_turn

    deps = QueryWorkerDeps(
        session_factory=factory,
        settings=MagicMock(),
    )
    with contextlib.suppress(Exception):
        await run_query_turn(
            conversation_id=uuid.uuid4(),
            user_message_content="q",
            deps=deps,
        )

    forwarded_types = {evt_type for _, evt_type, _ in captured_publishes}
    assert "_clarify_fallback" not in forwarded_types
    assert "research_plan" in forwarded_types
