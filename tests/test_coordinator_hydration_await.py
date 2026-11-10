"""Coordinator-side wiring for prior-subgraph hydration.

Exercises ``_await_prior_hydration`` — the helper that consumes the
hydration task started in run_research §1e, returns the
HydratedSubgraph for synthesis, and decides whether to emit the
prior_subgraph_hydrated SSE event.
"""

from __future__ import annotations

import asyncio

import pytest

from bifrost.research.coordinator import _await_prior_hydration
from bifrost.research.repo_agent import EntitySnippet
from bifrost.research.subgraph_hydrate import (
    HydratedRepoSubgraph,
    HydratedSubgraph,
)


def _make_snippet(*, urn: str, name: str = "X") -> EntitySnippet:
    return EntitySnippet(
        id=f"id-{name}",
        urn=urn,
        name=name,
        entity_type="component",
        purpose="",
    )


def _make_hydrated_nonempty() -> HydratedSubgraph:
    e = _make_snippet(urn="urn:test:E1")
    return HydratedSubgraph(
        repos=[HydratedRepoSubgraph(repo_id="r1", repo_name="orders", entities=[e])],
        total_entities=1,
        total_edges=0,
    )


def _make_hydrated_empty() -> HydratedSubgraph:
    return HydratedSubgraph(repos=[], total_entities=0, total_edges=0)


@pytest.mark.asyncio
async def test_returns_none_none_when_no_task() -> None:
    sub, event = await _await_prior_hydration(None)
    assert sub is None
    assert event is None


@pytest.mark.asyncio
async def test_returns_subgraph_and_event_on_happy_completion() -> None:
    expected = _make_hydrated_nonempty()

    async def _resolve():
        return expected

    task = asyncio.create_task(_resolve())
    sub, event = await _await_prior_hydration(task)

    assert sub is expected
    assert event is not None
    assert event["type"] == "prior_subgraph_hydrated"
    assert event["total_entities"] == 1
    assert event["repo_names"] == ["orders"]


@pytest.mark.asyncio
async def test_returns_subgraph_but_no_event_on_empty_hydration() -> None:
    """Empty hydration (URNs all gone) → still pass the subgraph
    through (synthesis will skip rendering since is_empty()), but
    don't emit a UI event for an empty result."""
    empty = _make_hydrated_empty()

    async def _resolve():
        return empty

    task = asyncio.create_task(_resolve())
    sub, event = await _await_prior_hydration(task)

    assert sub is empty
    assert event is None


@pytest.mark.asyncio
async def test_swallows_exception_and_returns_none_none() -> None:
    """Hydration failure (e.g. DB blip) is non-fatal — the chat still
    gets the live evidence + prior-bundle metadata, just no hydrated
    prior evidence section."""

    async def _crash():
        raise RuntimeError("DB unreachable")

    task = asyncio.create_task(_crash())
    sub, event = await _await_prior_hydration(task)

    assert sub is None
    assert event is None


@pytest.mark.asyncio
async def test_event_carries_summary_metadata() -> None:
    """The hydration event includes counts + missing-URN tally so
    the UI can show "12 entities + 23 edges from 2 repos"."""
    e = _make_snippet(urn="urn:test:E1")
    sub = HydratedSubgraph(
        repos=[
            HydratedRepoSubgraph(repo_id="r1", repo_name="alpha", entities=[e, e]),
            HydratedRepoSubgraph(repo_id="r2", repo_name="beta", entities=[e]),
        ],
        total_entities=3,
        total_edges=5,
        missing_urns=["gone-1"],
    )

    async def _resolve():
        return sub

    task = asyncio.create_task(_resolve())
    _, event = await _await_prior_hydration(task)

    assert event is not None
    assert event["total_entities"] == 3
    assert event["total_edges"] == 5
    assert event["repo_count"] == 2
    assert event["repo_names"] == ["alpha", "beta"]
    assert event["missing_urns"] == 1
