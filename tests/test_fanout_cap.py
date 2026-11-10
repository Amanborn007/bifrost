"""Tests for research depth tiering (split, never drop)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

# OBSOLETE: the depth-tiering helper `_split_rankings_by_depth` (deep/deferred
# "split, never drop" relevance split) was removed from
# bifrost.research.coordinator when fan-out scope-narrowing was refactored to
# the EXTEND_BUNDLE filtering model (`_maybe_narrow_to_extension`). The symbol
# no longer exists in production code, so this module can no longer be imported.
# Skipped at collection time rather than deleted, pending a rewrite against the
# new narrowing API.
pytest.skip(
    "depth-tiering helper _split_rankings_by_depth removed from coordinator "
    "(fan-out narrowing refactored to EXTEND_BUNDLE model)",
    allow_module_level=True,
)

from bifrost.research.coordinator import _split_rankings_by_depth  # noqa: E402


@dataclass
class _Rk:
    repo_id: str
    repo_name: str
    relevance: str


def _mk(*pairs: tuple[str, str]) -> list[_Rk]:
    return [_Rk(rid, rid, rel) for rid, rel in pairs]


def test_no_split_when_under_limit() -> None:
    rks = _mk(("a", "high"), ("b", "low"))
    deep, deferred = _split_rankings_by_depth(rks, 8)
    assert deep == rks and deferred == []


def test_splits_keeping_most_relevant_deep() -> None:
    rks = _mk(("a", "low"), ("b", "high"), ("c", "none"), ("d", "medium"), ("e", "high"))
    deep, deferred = _split_rankings_by_depth(rks, 3)
    # Top-3 by relevance are deep-searched…
    assert {r.repo_id for r in deep} == {"b", "e", "d"}
    # …the rest are DEFERRED (surfaced), never dropped — nothing is lost.
    assert {r.repo_id for r in deferred} == {"a", "c"}
    assert len(deep) + len(deferred) == len(rks)


def test_full_depth_researches_everything() -> None:
    rks = _mk(("a", "low"), ("b", "high"), ("c", "none"), ("d", "medium"))
    deep, deferred = _split_rankings_by_depth(rks, 2, full_depth=True)
    assert deep == rks and deferred == []  # nothing deferred under full depth


def test_unknown_relevance_treated_as_low() -> None:
    rks = _mk(("a", "high"), ("b", "weird"), ("c", "none"))
    deep, deferred = _split_rankings_by_depth(rks, 2)
    assert {r.repo_id for r in deep} == {"a", "b"}  # 'weird'≈low beats 'none'
    assert {r.repo_id for r in deferred} == {"c"}
