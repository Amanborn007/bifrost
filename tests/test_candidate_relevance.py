"""Relevance gate: scope-widening candidates are ranked by code relevance to
the question (not alphabetical), so junk/unrelated repos never get walked."""

from __future__ import annotations

import pytest

from bifrost.research.research_depth import rank_candidate_repos_by_relevance


@pytest.mark.asyncio
async def test_no_embedding_falls_back_to_input_order_capped():
    out = await rank_candidate_repos_by_relevance(
        None,
        ["a", "b", "c", "d"],
        None,
        keep=2,
    )
    assert out == ["a", "b"]


@pytest.mark.asyncio
async def test_empty_inputs():
    assert await rank_candidate_repos_by_relevance(None, [], [0.1], keep=3) == []
    assert await rank_candidate_repos_by_relevance(None, ["a"], [0.1], keep=0) == []
