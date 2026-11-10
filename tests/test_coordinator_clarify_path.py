"""Coordinator-level wiring for the CLARIFY shortcut.

Tests the extracted helpers ``_should_run_clarify_shortcut`` and
``_run_clarify_path`` so we can exercise the wrap-and-persist logic
without faking the full research-deps surface (scope resolution,
freshness ensure, candidate ranking, agent fan-out).

The bundle persistence is mocked via _persist_research_bundle; the
clarify generator itself is well-covered in test_clarify_from_bundle.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from bifrost.research.bundle_cache import BundleSource, ResearchBundle
from bifrost.research.coordinator import (
    _run_clarify_path,
    _should_run_clarify_shortcut,
)
from bifrost.research.planner import (
    MODE_CLARIFY,
    MODE_EXTEND_BUNDLE,
    MODE_NEW_SCOPE,
    ResearchPlan,
)


def _make_bundle(
    *,
    answer: str = (
        "It uses an exponential-backoff queue with jitter, scoped to a "
        "RetryQueue component that wraps the carrier-gateway client."
    ),
) -> ResearchBundle:
    return ResearchBundle(
        conversation_id="conv-prior",
        last_question="how does orders retry?",
        last_answer=answer,
        confidence="confirmed",
        repos_used=["00000000-0000-0000-0000-000000000001"],
        repos_used_names=["orders"],
        scope_rationale="single repo deep-indexed",
        subgraph_entity_urns=["bifrost://repo/orders/component/RetryQueue"],
        sources=[BundleSource(kind="jira", ref="SHIP-1", title="retry bug")],
        last_question_embedding=[0.1, 0.2, 0.3],
    )


# ── _should_run_clarify_shortcut ────────────────────────────────────


def test_runs_when_clarify_plan_and_high_similarity() -> None:
    plan = ResearchPlan(mode=MODE_CLARIFY, reason="x", similarity=0.95)
    assert _should_run_clarify_shortcut(plan, _make_bundle()) is True


def test_skips_when_plan_is_new_scope() -> None:
    plan = ResearchPlan(mode=MODE_NEW_SCOPE, reason="x", similarity=0.99)
    assert _should_run_clarify_shortcut(plan, _make_bundle()) is False


def test_skips_when_plan_is_extend_bundle() -> None:
    plan = ResearchPlan(mode=MODE_EXTEND_BUNDLE, reason="x", similarity=0.99)
    assert _should_run_clarify_shortcut(plan, _make_bundle()) is False


def test_skips_when_no_prior_bundle() -> None:
    plan = ResearchPlan(mode=MODE_CLARIFY, reason="x", similarity=0.99)
    assert _should_run_clarify_shortcut(plan, None) is False


def test_skips_when_similarity_below_floor() -> None:
    plan = ResearchPlan(mode=MODE_CLARIFY, reason="x", similarity=0.85)
    # 0.85 is below the 0.90 CLARIFY_SIMILARITY_FLOOR (planner says
    # CLARIFY, coordinator declines)
    assert _should_run_clarify_shortcut(plan, _make_bundle()) is False


def test_skips_when_prior_answer_too_short() -> None:
    plan = ResearchPlan(mode=MODE_CLARIFY, reason="x", similarity=0.99)
    assert _should_run_clarify_shortcut(plan, _make_bundle(answer="ok.")) is False


# ── _run_clarify_path ───────────────────────────────────────────────


@dataclass
class _FakeSettings:
    triage_model: str = "haiku"


def _make_runner(events: list[dict[str, Any]]):
    """Build a stand-in clarify_runner that yields the given events."""

    async def _runner(**_kw: Any) -> AsyncIterator[dict[str, Any]]:
        for e in events:
            yield e

    return _runner


@dataclass
class _PersistRecorder:
    calls: list[ResearchBundle] = field(default_factory=list)

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_persist(*, session_factory, conversation_id, bundle):  # noqa: ANN001
            self.calls.append(bundle)

        monkeypatch.setattr(
            "bifrost.research.coordinator._persist_research_bundle",
            _fake_persist,
        )


@pytest.mark.asyncio
async def test_happy_path_yields_events_and_persists_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _PersistRecorder()
    rec.patch(monkeypatch)
    runner = _make_runner(
        [
            {"type": "status", "message": "Reusing prior research…"},
            {"type": "synthesis_delta", "text": "It "},
            {"type": "synthesis_delta", "text": "is."},
            {
                "type": "done",
                "answer": "It is.",
                "sources": [{"urn": "SHIP-1"}],
                "confidence": "confirmed",
                "shortcut": "clarify_from_bundle",
            },
        ]
    )

    events: list[dict[str, Any]] = []
    bundle = _make_bundle()
    async for evt in _run_clarify_path(
        question="What does retry queue mean?",
        conversation_id=uuid.uuid4(),
        prior_bundle=bundle,
        question_embedding=[0.1, 0.2],
        prior_turns_text="",
        settings=_FakeSettings(),
        session_factory=None,  # type: ignore[arg-type]
        clarify_runner=runner,
    ):
        events.append(evt)

    types = [e["type"] for e in events]
    assert types == ["status", "synthesis_delta", "synthesis_delta", "done"]
    # Bundle persisted once with the streamed answer.
    assert len(rec.calls) == 1
    saved = rec.calls[0]
    assert saved.last_question == "What does retry queue mean?"
    assert saved.last_answer == "It is."
    assert saved.repos_used_names == ["orders"]  # carried from prior
    assert "clarify shortcut from prior bundle" in saved.scope_rationale


@pytest.mark.asyncio
async def test_haiku_failure_emits_fallback_and_skips_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _PersistRecorder()
    rec.patch(monkeypatch)
    runner = _make_runner(
        [
            {"type": "status", "message": "Reusing prior research…"},
            {"type": "error", "error": "Haiku rate-limited"},
        ]
    )

    events: list[dict[str, Any]] = []
    async for evt in _run_clarify_path(
        question="q",
        conversation_id=uuid.uuid4(),
        prior_bundle=_make_bundle(),
        question_embedding=None,
        prior_turns_text="",
        settings=_FakeSettings(),
        session_factory=None,  # type: ignore[arg-type]
        clarify_runner=runner,
    ):
        events.append(evt)

    types = [e["type"] for e in events]
    # The raw error is hidden — replaced with a status + fallback sentinel.
    assert "error" not in types
    assert "_clarify_fallback" in types
    assert any("Clarify shortcut couldn't" in str(e.get("message", "")) for e in events)
    # No bundle saved on failure.
    assert rec.calls == []


@pytest.mark.asyncio
async def test_no_done_event_emits_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the runner yielded deltas but never a 'done' event (e.g.
    the stream truncated mid-flight), we should also fall through."""
    rec = _PersistRecorder()
    rec.patch(monkeypatch)
    runner = _make_runner(
        [
            {"type": "status", "message": "x"},
            {"type": "synthesis_delta", "text": "partial"},
        ]
    )
    events: list[dict[str, Any]] = []
    async for evt in _run_clarify_path(
        question="q",
        conversation_id=uuid.uuid4(),
        prior_bundle=_make_bundle(),
        question_embedding=None,
        prior_turns_text="",
        settings=_FakeSettings(),
        session_factory=None,  # type: ignore[arg-type]
        clarify_runner=runner,
    ):
        events.append(evt)
    assert any(e["type"] == "_clarify_fallback" for e in events)
    assert rec.calls == []


@pytest.mark.asyncio
async def test_persist_failure_does_not_break_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If bundle persistence raises (e.g. DB down), the helper still
    completes — the user's answer has already been streamed and
    losing the bundle write is a follow-up-quality regression, not
    a chat-quality regression."""

    async def _broken_persist(**_kw: Any) -> None:
        raise RuntimeError("DB unreachable")

    monkeypatch.setattr(
        "bifrost.research.coordinator._persist_research_bundle",
        _broken_persist,
    )
    runner = _make_runner(
        [
            {"type": "synthesis_delta", "text": "ok"},
            {"type": "done", "answer": "ok", "sources": [], "confidence": "uncertain"},
        ]
    )
    events: list[dict[str, Any]] = []
    async for evt in _run_clarify_path(
        question="q",
        conversation_id=uuid.uuid4(),
        prior_bundle=_make_bundle(),
        question_embedding=None,
        prior_turns_text="",
        settings=_FakeSettings(),
        session_factory=None,  # type: ignore[arg-type]
        clarify_runner=runner,
    ):
        events.append(evt)
    # Stream completed normally; no fallback emitted.
    assert any(e["type"] == "done" for e in events)
    assert all(e["type"] != "_clarify_fallback" for e in events)
