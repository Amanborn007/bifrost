"""Fast-path answer route — unit tests.

Tests the trigger detection logic and the end-to-end happy path
without a running Postgres or LLM (all DB and LLM calls are patched).

Verifies:
  1. _detect_fast_path_intent correctly classifies questions.
  2. _try_fast_path returns None when no Postgres candidate is found.
  3. _try_fast_path returns a generator that yields correct SSE events
     when a known entity IS found in Postgres.
  4. Full-pipeline fallback when synthesis fails.
  5. The coordinator integration hook: the mock confirms run_research
     propagates fast-path events without calling the full pipeline
     for a known-entity question.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bifrost.research.fast_path import (
    _detect_fast_path_intent,
    _try_fast_path,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_settings(triage_model: str = "claude-haiku-4-5") -> Any:
    """Minimal settings stub."""
    s = MagicMock()
    s.triage_model = triage_model
    return s


def _make_entity(name: str = "OrderRouter", entity_type: str = "component") -> MagicMock:
    """Stub Entity ORM object."""
    e = MagicMock()
    e.id = uuid.uuid4()
    e.name = name
    e.entity_type = entity_type
    e.urn = f"urn:bifrost:repo/orders/component/{name}"
    e.qualified_name = f"com.acmeco.delivery.{name}"
    e.confidence = 0.9
    e.confidence_level = "confirmed"
    e.entity_metadata = {"language": "java", "file": "src/OrderRouter.java"}
    e.community_id = None
    e.scope_repo = MagicMock()
    e.scope_repo.name = "orders"
    return e


def _async_gen(*events: dict[str, Any]):
    """Build an async generator that yields the given dicts."""

    async def _gen() -> AsyncIterator[dict[str, Any]]:
        for evt in events:
            yield evt

    return _gen()


# ── 1. Trigger detection ──────────────────────────────────────────────────────


class TestDetectFastPathIntent:
    def test_entity_lookup_what_is(self) -> None:
        assert _detect_fast_path_intent("what is OrderRouter?") == "entity"

    def test_entity_lookup_what_does(self) -> None:
        assert _detect_fast_path_intent("what does OrderRouter do?") == "entity"

    def test_entity_lookup_describe(self) -> None:
        assert _detect_fast_path_intent("describe the RetryQueue component") == "entity"

    def test_entity_lookup_explain(self) -> None:
        assert _detect_fast_path_intent("explain how the ShippingRateService works") == "entity"

    def test_ownership_who_owns(self) -> None:
        assert _detect_fast_path_intent("who owns the orders repo?") == "ownership"

    def test_ownership_what_team(self) -> None:
        assert _detect_fast_path_intent("what team works on orders?") == "ownership"

    def test_ownership_what_squad(self) -> None:
        assert (
            _detect_fast_path_intent("what squad is responsible for webhook delivery?")
            == "ownership"
        )

    def test_cross_cutting_compare_blocked(self) -> None:
        assert _detect_fast_path_intent("compare OrderRouter vs RetryQueue") is None

    def test_cross_cutting_dependencies_blocked(self) -> None:
        assert _detect_fast_path_intent("what are the dependencies of OrderRouter?") is None

    def test_cross_cutting_blast_radius_blocked(self) -> None:
        assert _detect_fast_path_intent("what's the blast radius of changing OrderRouter?") is None

    def test_cross_cutting_interact_blocked(self) -> None:
        assert _detect_fast_path_intent("how do they interact across repos?") is None

    def test_unrecognised_question_returns_none(self) -> None:
        assert _detect_fast_path_intent("list all services") is None

    def test_flow_pipeline_blocked(self) -> None:
        assert _detect_fast_path_intent("show me the shipment flow pipeline") is None


# ── 2. _try_fast_path returns None when no DB candidate ───────────────────────


@pytest.mark.asyncio
async def test_try_fast_path_returns_none_for_cross_cutting_question() -> None:
    """Cross-cutting questions must return None without hitting Postgres."""
    settings = _make_settings()
    session_factory = MagicMock()

    result = await _try_fast_path(
        question="compare OrderRouter vs RetryQueue and their dependencies",
        conversation_id=uuid.uuid4(),
        session_factory=session_factory,
        settings=settings,
    )
    assert result is None
    # Session factory never touched — fast path short-circuits before DB hit
    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_try_fast_path_returns_none_when_no_entity_found() -> None:
    """Returns None when no entity matches in Postgres."""
    settings = _make_settings()

    async def _fake_session_ctx():
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        # Simulate empty result
        execute_result = MagicMock()
        execute_result.scalar_one_or_none = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=execute_result)
        return session

    # Build a minimal session_factory context manager
    sf = MagicMock()
    sf.return_value.__aenter__ = AsyncMock(
        return_value=AsyncMock(
            execute=AsyncMock(
                return_value=MagicMock(
                    scalar_one_or_none=MagicMock(return_value=None),
                    first=MagicMock(return_value=None),
                )
            ),
            refresh=AsyncMock(),
        )
    )
    sf.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await _try_fast_path(
        question="what is XYZNonExistentService?",
        conversation_id=uuid.uuid4(),
        session_factory=sf,
        settings=settings,
    )
    assert result is None


# ── 3. Happy path: entity found, synthesis streams correctly ──────────────────


@pytest.mark.asyncio
async def test_fast_path_entity_found_streams_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a known entity is found in Postgres and synthesis succeeds,
    the fast path yields scope + synthesis_delta + done events — without
    calling the full coordinator pipeline."""
    entity = _make_entity("OrderRouter", "component")
    settings = _make_settings()
    conversation_id = uuid.uuid4()

    # Patch _find_entity_by_name to return our stub entity
    monkeypatch.setattr(
        "bifrost.research.fast_path._find_entity_by_name",
        AsyncMock(return_value=entity),
    )
    # Patch _fetch_one_hop_relationships to return empty list
    monkeypatch.setattr(
        "bifrost.research.fast_path._fetch_one_hop_relationships",
        AsyncMock(return_value=[]),
    )

    # Build a session factory that returns a simple async session
    session_mock = AsyncMock()
    session_mock.refresh = AsyncMock()
    sf = MagicMock()
    sf.return_value.__aenter__ = AsyncMock(return_value=session_mock)
    sf.return_value.__aexit__ = AsyncMock(return_value=False)

    # Patch ClaudeLlmClient.stream_text to produce deterministic output
    async def _fake_stream(**_kw: Any) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "delta", "text": "OrderRouter "}
        yield {"type": "delta", "text": "routes orders to carriers."}
        yield {"type": "done", "usage": {}}

    events: list[dict[str, Any]] = []
    # The patch must wrap BOTH _try_fast_path and the generator iteration
    # because the generator is lazy — ClaudeLlmClient is instantiated
    # when the generator is iterated, not when _try_fast_path is called.
    with patch(
        "bifrost.research.fast_path.ClaudeLlmClient",
    ) as MockClient:
        instance = MockClient.return_value
        instance.stream_text = _fake_stream

        gen = await _try_fast_path(
            question="what is OrderRouter?",
            conversation_id=conversation_id,
            session_factory=sf,
            settings=settings,
        )

        assert gen is not None, "Expected a generator, got None"

        async for evt in gen:
            events.append(evt)

    types = [e["type"] for e in events]
    assert "scope" in types, f"Missing 'scope' event; got {types}"
    assert "synthesis_delta" in types, f"Missing 'synthesis_delta' event; got {types}"
    assert "done" in types, f"Missing 'done' event; got {types}"

    scope_evt = next(e for e in events if e["type"] == "scope")
    assert scope_evt.get("fast") is True
    assert scope_evt.get("route") == "orders"

    done_evt = next(e for e in events if e["type"] == "done")
    assert done_evt.get("fast_path") is True
    assert done_evt.get("confidence") == "graph-grounded"
    assert "OrderRouter" in done_evt.get("answer", "")

    # Sources should include the entity
    sources = done_evt.get("sources", [])
    assert len(sources) == 1
    assert sources[0]["kind"] == "entity"
    assert sources[0]["urn"] == entity.urn

    # No _fast_path_synthesis_error event
    assert all(e["type"] != "_fast_path_synthesis_error" for e in events)


# ── 4. Synthesis failure falls back via error sentinel ────────────────────────


@pytest.mark.asyncio
async def test_fast_path_synthesis_failure_emits_error_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LLM synthesis fails, the generator should emit a
    _fast_path_synthesis_error event so the caller can fall through."""
    entity = _make_entity("OrderRouter", "component")
    settings = _make_settings()

    monkeypatch.setattr(
        "bifrost.research.fast_path._find_entity_by_name",
        AsyncMock(return_value=entity),
    )
    monkeypatch.setattr(
        "bifrost.research.fast_path._fetch_one_hop_relationships",
        AsyncMock(return_value=[]),
    )

    session_mock = AsyncMock()
    session_mock.refresh = AsyncMock()
    sf = MagicMock()
    sf.return_value.__aenter__ = AsyncMock(return_value=session_mock)
    sf.return_value.__aexit__ = AsyncMock(return_value=False)

    async def _failing_stream(**_kw: Any) -> AsyncIterator[dict[str, Any]]:
        raise RuntimeError("LLM unavailable")
        yield  # make it an async generator

    events: list[dict[str, Any]] = []
    with patch("bifrost.research.fast_path.ClaudeLlmClient") as MockClient:
        instance = MockClient.return_value
        instance.stream_text = _failing_stream

        gen = await _try_fast_path(
            question="what is OrderRouter?",
            conversation_id=uuid.uuid4(),
            session_factory=sf,
            settings=settings,
        )

        assert gen is not None

        async for evt in gen:
            events.append(evt)

    error_evts = [e for e in events if e["type"] == "_fast_path_synthesis_error"]
    assert len(error_evts) == 1
    # The error text should contain "LLM unavailable"
    assert "LLM unavailable" in error_evts[0]["error"]


# ── 5. Coordinator integration: fast-path short-circuits run_research ─────────


@pytest.mark.asyncio
async def test_coordinator_run_research_uses_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_research() should yield fast-path events and return early when
    _try_fast_path finds a match — without calling into scope resolution,
    freshness, or agent fan-out."""

    # Build a deterministic fast-path generator
    async def _fast_gen() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "scope", "fast": True, "route": "orders", "candidates": []}
        yield {"type": "synthesis_delta", "text": "OrderRouter handles routing."}
        yield {
            "type": "done",
            "answer": "OrderRouter handles routing.",
            "sources": [],
            "confidence": "graph-grounded",
            "fast_path": True,
        }

    # Patch _try_fast_path in the fast_path module — the coordinator does
    # a local import inside run_research, so patching the module where
    # the name lives is the correct target.
    monkeypatch.setattr(
        "bifrost.research.fast_path._try_fast_path",
        AsyncMock(return_value=_fast_gen()),
    )

    # We need a minimal ResearchDeps — session_factory and settings
    # are never actually reached because the fast path intercepts first
    from bifrost.research.coordinator import ResearchDeps, run_research

    @dataclass
    class _FakeSettings:
        triage_model: str = "haiku"
        agent_model: str = "sonnet"

    deps = ResearchDeps(
        session_factory=MagicMock(),
        settings=_FakeSettings(),  # type: ignore[arg-type]
    )

    events: list[dict[str, Any]] = []
    async for evt in run_research(
        question="what is OrderRouter?",
        conversation_id=uuid.uuid4(),
        deps=deps,
    ):
        events.append(evt)

    types = [e["type"] for e in events]
    assert "scope" in types
    assert "synthesis_delta" in types
    assert "done" in types

    done = next(e for e in events if e["type"] == "done")
    assert done.get("fast_path") is True
    assert done.get("confidence") == "graph-grounded"

    # The full pipeline must not have been reached; verify by absence of
    # the "Resolving scope…" status message (which comes from step 1 of
    # the full pipeline, after the fast-path check).
    status_messages = [e.get("message", "") for e in events if e["type"] == "status"]
    assert not any("Resolving scope" in m for m in status_messages), (
        "Full pipeline was invoked — fast path did not short-circuit correctly"
    )
