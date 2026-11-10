"""Stored values, API fields, prompts and UI strings carry no banned name.

The names come from LEAK_DENYLIST via tests/leak_terms.py; these checks skip when it is unset.
Source and UI files are scanned by test_leak_gate.
"""

from __future__ import annotations

import dataclasses
import typing

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from tests import leak_terms

# ── Stored enum values ─────────────────────────────────────────────


def test_handover_scope_kind_is_neutral() -> None:
    from bifrost.api.schemas.collaboration import HandoverScopeKind

    assert set(typing.get_args(HandoverScopeKind)) == {
        "feature",
        "repo",
        "domain",
        "region",
        "team",
        "session",
    }


def test_repo_status_is_neutral() -> None:
    from bifrost.research.status import RepoStatus, status_summary

    assert set(typing.get_args(RepoStatus)) == {
        "deep_indexed",
        "manifest_only",
        "not_cloned",
        "auto_registered",
    }
    assert set(status_summary({})) == {
        "deep_indexed",
        "manifest_only",
        "not_cloned",
        "auto_registered",
    }


def test_status_helpers_are_neutral() -> None:
    from bifrost.research import status

    assert hasattr(status, "manifest_only_ids")
    assert [n for n in dir(status) if leak_terms.matches(n)] == []


def test_trigger_kind_literal_is_neutral() -> None:
    from bifrost.actions.tools import bifrost_trigger_extraction

    hint = typing.get_type_hints(bifrost_trigger_extraction)["trigger_kind"]
    assert set(typing.get_args(hint)) == {
        "auto_manifest_only",
        "ask_back_gap",
        "user_requested",
        "scheduled",
    }


def test_discussion_assumption_source_enum_is_neutral() -> None:
    from bifrost.actions.discussion_agent import _DISCUSSION_SCHEMA

    items = _DISCUSSION_SCHEMA["properties"]["assumptions"]["items"]
    enum = items["properties"]["source_type"]["enum"]
    assert "manifest" in enum
    assert [v for v in enum if leak_terms.matches(v)] == []


def test_confidence_policy_known_types_are_neutral() -> None:
    from bifrost.research.confidence_policy import _TIER_MAP, SourceType

    assert "manifest" in typing.get_args(SourceType)
    assert _TIER_MAP["manifest"] == ("confirmed", 0.88)
    known = [*typing.get_args(SourceType), *_TIER_MAP]
    assert [v for v in known if leak_terms.matches(v)] == []


# ── API and ORM names ──────────────────────────────────────────────


def test_catalog_results_use_manifest_hits() -> None:
    from bifrost.api.routes.catalog import CatalogSyncResponse
    from bifrost.research.org_catalog import CatalogSyncResult

    assert "manifest_hits" in CatalogSyncResponse.model_fields
    assert "manifest_hits" in {f.name for f in dataclasses.fields(CatalogSyncResult)}
    names = set(CatalogSyncResponse.model_fields) | {
        f.name for f in dataclasses.fields(CatalogSyncResult)
    }
    assert [n for n in names if leak_terms.matches(n)] == []


def test_orm_columns_and_checks_carry_no_banned_name() -> None:
    from sqlalchemy import CheckConstraint

    from bifrost.storage.models import Base

    columns = [f"{t.name}.{c.name}" for t in Base.metadata.tables.values() for c in t.columns]
    assert [c for c in columns if leak_terms.matches(c)] == []
    checks = [
        str(con.sqltext)
        for t in Base.metadata.tables.values()
        for con in t.constraints
        if isinstance(con, CheckConstraint)
    ]
    assert [c for c in checks if leak_terms.matches(c)] == []


def test_orm_uses_manifest_columns() -> None:
    from bifrost.storage.models import Base

    ownership = Base.metadata.tables["repo_ownership"]
    assert "raw_manifest" in ownership.c
    assert ownership.c.source.server_default.arg == "manifest"
    org_repos = Base.metadata.tables["org_repos"]
    assert {"manifest_fetched_at", "manifest_error"} <= set(org_repos.c.keys())


# ── Handover team scope ────────────────────────────────────────────


def test_team_handover_symbols_are_neutral() -> None:
    from bifrost.api.schemas import collaboration as schemas
    from bifrost.collaboration import cost_estimate, handover_generator

    assert callable(handover_generator.generate_team_handover)
    assert callable(cost_estimate.estimate_team_handover_cost)
    assert isinstance(handover_generator._TEAM_HANDOVER_SYSTEM_PROMPT, str)
    assert hasattr(schemas, "GenerateTeamHandoverRequest")
    for module in (handover_generator, cost_estimate, schemas):
        assert [n for n in dir(module) if leak_terms.matches(n)] == []


async def test_stream_route_rejects_an_unknown_scope_name() -> None:
    from bifrost.api.routes.handover_docs import stream_handover_generation

    with pytest.raises(HTTPException) as exc:
        await stream_handover_generation("galaxy", "squad-a")
    assert exc.value.status_code == 400


async def test_stream_route_accepts_team() -> None:
    from bifrost.api.routes.handover_docs import stream_handover_generation

    resp = await stream_handover_generation("team", "squad-a")
    assert isinstance(resp, StreamingResponse)


# ── LLM-visible text ───────────────────────────────────────────────


def test_tier_badge_says_manifest() -> None:
    from bifrost.research.coordinator import _render_evidence_block
    from bifrost.research.repo_agent import EntitySnippet, GraphEdge

    edge = GraphEdge(
        rel_type="depends-on",
        other_name="B",
        other_urn="urn:bifrost:repo/repo-a/component/B",
        other_type="component",
        confidence=0.88,
        source_type="manifest",
        tier=2,
    )
    entity = EntitySnippet(
        name="A",
        entity_type="component",
        purpose="p",
        source_file="src/A.py",
        id="a-id",
        urn="urn:bifrost:repo/repo-a/component/A",
        outbound=[edge],
        inbound=[],
    )
    block = _render_evidence_block([entity])
    assert "[T2 manifest]" in block
    assert not leak_terms.matches(block)


def test_prompt_constants_are_neutral() -> None:
    from bifrost.actions.discussion_agent import _DISCUSSION_SYSTEM_PROMPT
    from bifrost.collaboration import handover_generator
    from bifrost.research.confidence_policy import CODE_IS_TRUTH_POLICY
    from bifrost.research.deepening import _FOLLOWUP_SYSTEM
    from bifrost.research.fast_path import _OWNERSHIP_SYNTHESIS_TEMPLATE

    prompts = {
        "_DISCUSSION_SYSTEM_PROMPT": _DISCUSSION_SYSTEM_PROMPT,
        "CODE_IS_TRUTH_POLICY": CODE_IS_TRUTH_POLICY,
        "_FOLLOWUP_SYSTEM": _FOLLOWUP_SYSTEM,
        "_OWNERSHIP_SYNTHESIS_TEMPLATE": _OWNERSHIP_SYNTHESIS_TEMPLATE,
    }
    for name, value in vars(handover_generator).items():
        if name.endswith("_PROMPT") and isinstance(value, str):
            prompts[name] = value
    assert [n for n, v in prompts.items() if leak_terms.matches(v)] == []
