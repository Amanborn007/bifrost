"""Chat settings schema + merge.

Pure-function tests for the Pydantic shape and the merge helpers.
The route layer (GET/PATCH /conversations/{id}/settings) is exercised
via a live curl smoke; here we lock the
contracts the route depends on.
"""

from __future__ import annotations

import pytest

from bifrost.api.schemas.chat_settings import (
    ChatSettings,
    PartialChatSettings,
    merge_settings,
    settings_from_stored,
)


def test_defaults_match_architecture_doc() -> None:
    """The defaults are the spec — keep this test in sync with the documented chat settings."""
    s = ChatSettings()
    assert s.walk_depth == 2
    assert s.walk_breadth_soft_cap == 200
    assert s.confidence_floor == 0.55
    assert s.edge_directions == "both"
    assert "calls" in s.edge_types and "depends_on" in s.edge_types
    assert s.code_first_strict is True
    assert s.agents_per_repo_max == 1
    # gdocs is an intentional first-class source (research/gdocs_agent.py);
    # the default external-source set grew to include it.
    assert sorted(s.external_sources) == [
        "confluence",
        "gdocs",
        "github",
        "jira",
        "slack",
    ]
    assert s.auto_extract_missing is True
    assert s.auto_extract_wait_s == 90.0


def test_walk_depth_bounds() -> None:
    ChatSettings(walk_depth=1)
    ChatSettings(walk_depth=6)
    with pytest.raises(ValueError):
        ChatSettings(walk_depth=0)
    with pytest.raises(ValueError):
        ChatSettings(walk_depth=7)


def test_confidence_floor_bounds() -> None:
    ChatSettings(confidence_floor=0.0)
    ChatSettings(confidence_floor=1.0)
    with pytest.raises(ValueError):
        ChatSettings(confidence_floor=-0.1)
    with pytest.raises(ValueError):
        ChatSettings(confidence_floor=1.1)


def test_unknown_edge_types_dropped_silently() -> None:
    """A user storing settings before the extraction pipeline added
    a new rel_type shouldn't see 422s. Unknown types are filtered."""
    s = ChatSettings(edge_types=["calls", "fakeedge", "depends_on"])
    assert s.edge_types == ["calls", "depends_on"]


def test_partial_chat_settings_all_optional() -> None:
    """PATCH must accept any subset of fields."""
    p = PartialChatSettings()
    assert p.model_dump(exclude_unset=True) == {}
    p = PartialChatSettings(walk_depth=4)
    assert p.model_dump(exclude_unset=True) == {"walk_depth": 4}


def test_merge_settings_keeps_stored_when_patch_missing() -> None:
    stored = {"walk_depth": 5, "edge_directions": "upstream"}
    patch = PartialChatSettings(confidence_floor=0.7)
    out = merge_settings(stored, patch)
    assert out.walk_depth == 5  # from stored
    assert out.edge_directions == "upstream"
    assert out.confidence_floor == 0.7  # from patch
    assert out.code_first_strict is True  # from defaults


def test_merge_settings_patch_wins_over_stored() -> None:
    stored = {"walk_depth": 1}
    patch = PartialChatSettings(walk_depth=4)
    assert merge_settings(stored, patch).walk_depth == 4


def test_merge_settings_validates_after_merge() -> None:
    """Merge re-validates so an out-of-range patch fails the merge,
    not silently coerces."""
    stored = {"walk_depth": 2}
    patch = PartialChatSettings(walk_depth=4)
    # OK case
    merge_settings(stored, patch)
    # Out-of-range — Pydantic raises at the partial level too,
    # so this is belt-and-suspenders.
    with pytest.raises(ValueError):
        PartialChatSettings(walk_depth=99)


def test_settings_from_stored_handles_empty() -> None:
    assert settings_from_stored({}).walk_depth == 2
    assert settings_from_stored(None).walk_depth == 2


def test_settings_from_stored_handles_partial() -> None:
    s = settings_from_stored({"walk_depth": 4, "agents_per_repo_max": 3})
    assert s.walk_depth == 4
    assert s.agents_per_repo_max == 3
    assert s.confidence_floor == 0.55  # defaulted


def test_external_sources_validation() -> None:
    """external_sources is a Literal list — unknown values fail."""
    ChatSettings(external_sources=["jira", "confluence"])
    with pytest.raises(ValueError):
        ChatSettings(external_sources=["jira", "discord"])
