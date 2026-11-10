"""Coordinator EXTEND_BUNDLE narrow-fan-out decision.

Exercises ``_maybe_narrow_for_extend`` — the helper that decides
whether the rankings should be filtered to the planner's extension
repos before agent fan-out. Decoupled from the full coordinator so
the decision logic is unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from bifrost.research.bundle_cache import BundleSource, ResearchBundle
from bifrost.research.coordinator import _maybe_narrow_for_extend
from bifrost.research.planner import (
    MODE_CLARIFY,
    MODE_EXTEND_BUNDLE,
    MODE_NEW_SCOPE,
    ResearchPlan,
)


@dataclass
class _Ranking:
    repo_id: str
    repo_name: str
    relevance: str = "high"
    reason: str = "x"


def _make_bundle(repos: list[str] | None = None) -> ResearchBundle:
    return ResearchBundle(
        conversation_id="conv",
        last_question="q",
        last_answer="a" * 100,
        confidence="confirmed",
        repos_used=["00000000-0000-0000-0000-000000000001"],
        repos_used_names=repos or ["orders"],
        scope_rationale="x",
        subgraph_entity_urns=["urn:1"],
        sources=[BundleSource(kind="jira", ref="SHIP-1")],
    )


def _rankings(*ids: str) -> list[_Ranking]:
    return [_Ranking(repo_id=rid, repo_name=f"r-{rid}") for rid in ids]


def test_narrows_when_extend_plan_with_extension_repos() -> None:
    rankings = _rankings("a", "b", "c")
    plan = ResearchPlan(
        mode=MODE_EXTEND_BUNDLE,
        reason="x",
        similarity=0.7,
        extension_repos=["c"],
        extension_repo_names=["r-c"],
    )
    out_rankings, event = _maybe_narrow_for_extend(
        rankings,
        plan,
        _make_bundle(repos=["orders", "auth"]),
    )
    assert event is not None
    assert event["type"] == "scope_narrowed"
    assert event["from_count"] == 3
    assert event["to_count"] == 1
    assert event["to_repos"] == ["r-c"]
    assert event["prior_repos"] == ["orders", "auth"]
    assert [r.repo_id for r in out_rankings] == ["c"]


def test_no_narrow_when_plan_is_new_scope() -> None:
    rankings = _rankings("a", "b")
    plan = ResearchPlan(
        mode=MODE_NEW_SCOPE,
        reason="x",
        extension_repos=["a"],
    )
    out, event = _maybe_narrow_for_extend(rankings, plan, _make_bundle())
    assert event is None
    assert out is rankings  # unchanged


def test_no_narrow_when_plan_is_clarify() -> None:
    rankings = _rankings("a", "b")
    plan = ResearchPlan(
        mode=MODE_CLARIFY,
        reason="x",
        extension_repos=["a"],
    )
    out, event = _maybe_narrow_for_extend(rankings, plan, _make_bundle())
    assert event is None
    assert out is rankings


def test_no_narrow_when_no_extension_repos() -> None:
    """EXTEND_BUNDLE without identified extension repos → fall through
    so we don't accidentally produce an empty rankings list."""
    rankings = _rankings("a", "b")
    plan = ResearchPlan(
        mode=MODE_EXTEND_BUNDLE,
        reason="x",
        extension_repos=[],
    )
    out, event = _maybe_narrow_for_extend(rankings, plan, _make_bundle())
    assert event is None
    assert out is rankings


def test_no_narrow_when_no_prior_bundle() -> None:
    """Defensive: if classification is EXTEND but there's no prior
    bundle to reuse, narrowing would lose information."""
    rankings = _rankings("a")
    plan = ResearchPlan(
        mode=MODE_EXTEND_BUNDLE,
        reason="x",
        extension_repos=["a"],
    )
    out, event = _maybe_narrow_for_extend(rankings, plan, None)
    assert event is None
    assert out is rankings


def test_no_narrow_when_extension_not_in_rankings() -> None:
    """If the planner suggested repos that the router didn't even pick,
    fall through to full pipeline rather than emit empty rankings."""
    rankings = _rankings("a", "b")
    plan = ResearchPlan(
        mode=MODE_EXTEND_BUNDLE,
        reason="x",
        extension_repos=["zz"],  # not in rankings
    )
    out, event = _maybe_narrow_for_extend(rankings, plan, _make_bundle())
    assert event is None
    assert out is rankings


def test_narrow_keeps_all_when_all_in_extension_set() -> None:
    rankings = _rankings("a", "b", "c")
    plan = ResearchPlan(
        mode=MODE_EXTEND_BUNDLE,
        reason="x",
        extension_repos=["a", "b", "c"],
    )
    out, event = _maybe_narrow_for_extend(rankings, plan, _make_bundle())
    assert event is not None
    assert event["from_count"] == 3
    assert event["to_count"] == 3
    assert [r.repo_id for r in out] == ["a", "b", "c"]


def test_message_mentions_prior_repo_count() -> None:
    rankings = _rankings("c")
    plan = ResearchPlan(
        mode=MODE_EXTEND_BUNDLE,
        reason="x",
        extension_repos=["c"],
    )
    bundle = _make_bundle(repos=["orders", "auth", "batch-orders"])
    _, event = _maybe_narrow_for_extend(rankings, plan, bundle)
    assert event is not None
    assert "3 repo(s) already walked" in event["message"]
