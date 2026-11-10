"""Orchestrator agent tests.

Pure-function tests for ``OrchestrationPlan`` shape +
``merge_plan_with_settings`` precedence. The actual Haiku call in
``run_orchestrator`` is exercised by the live coordinator path; we
test only the shape, fallback behaviour, and merge logic here.
"""

from __future__ import annotations

from bifrost.api.schemas.chat_settings import ChatSettings
from bifrost.research.orchestrator import (
    OrchestrationPlan,
    ResolvedWalkParams,
    merge_plan_with_settings,
)

# ── Shape contract ──────────────────────────────────────────────


def test_orchestration_plan_defaults() -> None:
    """All optional fields default to None / empty so the merge
    layer can detect "no opinion"."""
    p = OrchestrationPlan(intent="discovery")
    assert p.seed_phrases == []
    assert p.target_repos == []
    assert p.target_squads == []
    assert p.walk_depth is None
    assert p.walk_breadth_soft_cap is None
    assert p.confidence_floor is None
    assert p.edge_directions is None
    assert p.edge_type_overrides is None
    assert p.reasoning == ""
    assert p.is_fallback is False


# ── Merge: user touched settings → settings always win ──────────


def test_merge_user_touched_settings_win_over_plan() -> None:
    """When the user has PATCHed settings, they always win for walk
    parameters. The plan still contributes intent + reasoning at
    the call site, but those aren't part of ResolvedWalkParams."""
    settings = ChatSettings(
        walk_depth=5,
        confidence_floor=0.85,
        edge_directions="upstream",
    )
    plan = OrchestrationPlan(
        intent="impact_radius",
        walk_depth=2,  # plan wants 2
        confidence_floor=0.55,  # plan wants 0.55
        edge_directions="downstream",  # plan wants downstream
    )
    resolved = merge_plan_with_settings(
        plan,
        settings,
        user_touched_settings=True,
    )
    # Settings won every walk parameter
    assert resolved.walk_depth == 5
    assert resolved.confidence_floor == 0.85
    assert resolved.edge_directions == "upstream"


# ── Merge: untouched settings → plan overrides defaults ─────────


def test_merge_plan_overrides_defaults_when_user_untouched() -> None:
    """When the user hasn't touched settings (empty stored JSONB),
    plan parameters take effect over factory defaults."""
    settings = ChatSettings()  # all defaults
    plan = OrchestrationPlan(
        intent="impact_radius",
        walk_depth=4,
        confidence_floor=0.75,
        edge_directions="downstream",
    )
    resolved = merge_plan_with_settings(
        plan,
        settings,
        user_touched_settings=False,
    )
    assert resolved.walk_depth == 4
    assert resolved.confidence_floor == 0.75
    assert resolved.edge_directions == "downstream"


def test_merge_plan_partial_falls_back_to_defaults() -> None:
    """Plan with some fields None falls back to settings defaults
    for those fields."""
    settings = ChatSettings()
    plan = OrchestrationPlan(
        intent="discovery",
        walk_depth=3,  # opinion
        # confidence_floor / directions / etc → None
    )
    resolved = merge_plan_with_settings(
        plan,
        settings,
        user_touched_settings=False,
    )
    assert resolved.walk_depth == 3  # from plan
    assert resolved.confidence_floor == 0.55  # from default
    assert resolved.edge_directions == "both"  # from default


def test_merge_plan_zero_floor_distinguishes_from_none() -> None:
    """confidence_floor=0.0 is a meaningful opinion (no floor); it
    must NOT be confused with None (no opinion)."""
    settings = ChatSettings(confidence_floor=0.55)
    plan = OrchestrationPlan(intent="discovery", confidence_floor=0.0)
    resolved = merge_plan_with_settings(
        plan,
        settings,
        user_touched_settings=False,
    )
    # Plan said 0.0 explicitly → use it.
    assert resolved.confidence_floor == 0.0


def test_merge_no_opinion_plan_yields_factory_defaults() -> None:
    """Plan with all walk fields None + untouched settings → every
    resolved field equals the factory default."""
    settings = ChatSettings()
    plan = OrchestrationPlan(intent="discovery")
    resolved = merge_plan_with_settings(
        plan,
        settings,
        user_touched_settings=False,
    )
    assert resolved.walk_depth == 2
    assert resolved.walk_breadth_soft_cap == 200
    assert resolved.confidence_floor == 0.55
    assert resolved.edge_directions == "both"
    assert "calls" in resolved.edge_types


# ── Edge type overrides ─────────────────────────────────────────


def test_merge_edge_type_overrides_replace_settings() -> None:
    """Plan's edge_type_overrides REPLACE (not extend) settings'
    edge_types when user untouched."""
    settings = ChatSettings()  # default = full list
    plan = OrchestrationPlan(
        intent="discovery",
        edge_type_overrides=["produces_event", "consumes_event"],
    )
    resolved = merge_plan_with_settings(
        plan,
        settings,
        user_touched_settings=False,
    )
    assert resolved.edge_types == ["produces_event", "consumes_event"]


def test_merge_no_edge_overrides_uses_settings() -> None:
    """Plan with edge_type_overrides=None falls back to settings."""
    settings = ChatSettings(edge_types=["calls", "depends_on"])
    plan = OrchestrationPlan(intent="discovery")
    resolved = merge_plan_with_settings(
        plan,
        settings,
        user_touched_settings=False,
    )
    assert resolved.edge_types == ["calls", "depends_on"]


# ── Returned shape ──────────────────────────────────────────────


def test_resolved_walk_params_shape() -> None:
    """ResolvedWalkParams has the exact 5 fields the coordinator
    consumes."""
    settings = ChatSettings()
    plan = OrchestrationPlan(intent="discovery")
    resolved = merge_plan_with_settings(
        plan,
        settings,
        user_touched_settings=False,
    )
    assert isinstance(resolved, ResolvedWalkParams)
    # Every coordinator-consumed field is present
    assert hasattr(resolved, "walk_depth")
    assert hasattr(resolved, "walk_breadth_soft_cap")
    assert hasattr(resolved, "confidence_floor")
    assert hasattr(resolved, "edge_directions")
    assert hasattr(resolved, "edge_types")


# ── Fallback plan stays intact ───────────────────────────────────


def test_fallback_plan_can_still_merge() -> None:
    """When run_orchestrator falls back, the resulting plan has
    is_fallback=True and all walk fields None. Merge should
    produce factory defaults — research continues unimpeded."""
    settings = ChatSettings()
    fallback = OrchestrationPlan(
        intent="discovery",
        reasoning="Orchestrator LLM failed; using settings defaults.",
        is_fallback=True,
    )
    resolved = merge_plan_with_settings(
        fallback,
        settings,
        user_touched_settings=False,
    )
    assert resolved.walk_depth == 2  # default
    assert resolved.confidence_floor == 0.55
