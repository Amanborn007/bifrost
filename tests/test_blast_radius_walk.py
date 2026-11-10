"""Confidence-aware BFS walk.

Pure-shape tests for the new ``expand_subgraph`` parameters + projection
contract, PLUS three fixture-graph integration tests covering the
behaviours the walk spec called for explicitly:

  * confidence_floor=0.9 drops below-floor edges
  * direction="upstream" returns only the parents subset
  * soft_cap=N with N+M candidates returns all N+M + flags exceeded

The integration tests use the live dev DB with a unique URN
namespace (``urn:test:s02:*``) and clean up in a finally block so
multiple runs don't leak fixture rows.
"""

from __future__ import annotations

import uuid as _uuid

import pytest

from bifrost.research.graph_retrieval import (
    Subgraph,
    SubgraphEdge,
    SubgraphNode,
    expand_subgraph,
)


def _make_node(
    nid: str,
    *,
    name: str = "n",
    confidence: float = 1.0,
) -> SubgraphNode:
    return SubgraphNode(
        id=nid,
        urn=f"urn:test:{nid}",
        name=name,
        entity_type="component",
        purpose="test fixture",
        source_file=None,
        metadata={},
        repo_id="repo-x",
        distance_from_seed=1,
        degree=0,
        is_seed=False,
        min_path_confidence=confidence,
    )


def _make_edge(
    s: str,
    t: str,
    *,
    confidence: float = 0.95,
    direction: str | None = "downstream",
) -> SubgraphEdge:
    return SubgraphEdge(
        source_id=s,
        target_id=t,
        rel_type="calls",
        source_urn=f"urn:test:{s}",
        source_name=s,
        source_repo_id="repo-x",
        target_urn=f"urn:test:{t}",
        target_name=t,
        target_repo_id="repo-y",
        is_cross_repo=True,
        confidence=confidence,
        source_type="code_verified",
        tier=1,
        direction_from_seed=direction,
    )


# ── confidence_buckets projection ────────────────────────────────


def test_confidence_buckets_partitions_by_path_confidence() -> None:
    g = Subgraph(
        nodes=[
            _make_node("h1", confidence=0.95),
            _make_node("h2", confidence=0.88),
            _make_node("m1", confidence=0.70),
            _make_node("m2", confidence=0.55),
            _make_node("l1", confidence=0.40),
            _make_node("l2", confidence=0.10),
        ],
    )
    buckets = g.confidence_buckets
    assert sorted(buckets["high"]) == ["h1", "h2"]
    assert sorted(buckets["medium"]) == ["m1", "m2"]
    assert sorted(buckets["low"]) == ["l1", "l2"]


def test_confidence_buckets_handles_empty() -> None:
    g = Subgraph()
    assert g.confidence_buckets == {"high": [], "medium": [], "low": []}


def test_confidence_buckets_treats_seeds_as_high() -> None:
    """Seeds default to min_path_confidence=1.0 — they should land
    in the ``high`` bucket regardless of any inbound edges."""
    n = _make_node("seed", confidence=1.0)
    n.is_seed = True
    g = Subgraph(nodes=[n], seeds=["seed"])
    assert "seed" in g.confidence_buckets["high"]


# ── soft_cap_exceeded flag ───────────────────────────────────────


def test_soft_cap_exceeded_default_false() -> None:
    g = Subgraph()
    assert g.soft_cap_exceeded is False


def test_soft_cap_exceeded_can_be_set() -> None:
    g = Subgraph(soft_cap_exceeded=True)
    assert g.soft_cap_exceeded is True


# ── direction_from_seed on edges ─────────────────────────────────


def test_edge_direction_field_round_trip() -> None:
    e = _make_edge("a", "b", direction="upstream")
    assert e.direction_from_seed == "upstream"
    e2 = _make_edge("a", "b", direction=None)
    assert e2.direction_from_seed is None


# ── min_path_confidence semantics ────────────────────────────────


def test_min_path_confidence_default_for_seeds() -> None:
    """Seeds carry confidence 1.0 by default — the BFS code in
    ``expand_subgraph`` sets this explicitly. Pure-shape test that
    the dataclass default cooperates."""
    n = SubgraphNode(
        id="s",
        urn="urn:test:s",
        name="seed",
        entity_type="component",
        purpose="",
        source_file=None,
        metadata={},
        repo_id="r",
        distance_from_seed=0,
        degree=0,
        is_seed=True,
    )
    # Default is 1.0 per the dataclass declaration.
    assert n.min_path_confidence == 1.0


# ── expand_subgraph signature smoke ──────────────────────────────


def test_expand_subgraph_accepts_new_kwargs() -> None:
    """Don't actually walk a DB — just verify the signature accepts
    the new kwargs without TypeError."""
    import inspect

    from bifrost.research.graph_retrieval import expand_subgraph

    sig = inspect.signature(expand_subgraph)
    params = sig.parameters
    assert "confidence_floor" in params
    assert "direction" in params
    assert "soft_cap" in params
    # Back-compat alias still accepted
    assert "max_nodes" in params
    # Defaults match the walk spec
    assert params["confidence_floor"].default == 0.0
    assert params["direction"].default == "both"
    assert params["soft_cap"].default == 200


# ── Fixture-graph integration tests ───────────────────────────
#
# Use the live dev DB. Each test inserts a small graph under a
# unique URN namespace, runs ``expand_subgraph``, asserts behavior,
# and cleans up in a finally block so the dev DB is never left
# polluted across runs.


@pytest.fixture
async def fixture_session():
    """Async DB session against the live dev DB. Keep transactions
    short so we don't block migrations or other concurrent writes."""
    from bifrost.api.deps import get_session_factory
    from bifrost.cli.config import get_settings

    factory = get_session_factory(get_settings())
    async with factory() as session:
        yield session


async def _insert_fixture_graph(session, *, prefix: str) -> dict[str, str]:
    """Insert a small graph via ORM. Returns {label: entity_id (str)}."""
    from bifrost.storage.models import Entity, EntityRelationship

    nodes = {
        "seed": _uuid.uuid4(),
        "high": _uuid.uuid4(),  # connected via 0.95 edge
        "med": _uuid.uuid4(),  # connected via 0.70 edge
        "low": _uuid.uuid4(),  # connected via 0.40 edge
    }
    for label, eid in nodes.items():
        session.add(
            Entity(
                id=eid,
                entity_type="component",
                urn=f"urn:test:s02:{prefix}:{label}",
                name=f"{prefix}-{label}",
                qualified_name=f"{prefix}.{label}",
                knowledge_tier=1,
                entity_metadata={},
                confidence=0.9,
                confidence_level="confirmed",
                source_basis=[],
                freshness_state="fresh",
            )
        )
    edges = [
        ("seed", "high", 0.95, "code_verified", 1),
        ("seed", "med", 0.70, "metadata_match", 3),
        ("seed", "low", 0.40, "fuzzy_match", 5),
    ]
    for src, dst, conf, src_type, tier in edges:
        session.add(
            EntityRelationship(
                id=_uuid.uuid4(),
                source_id=nodes[src],
                target_id=nodes[dst],
                rel_type="calls",
                rel_metadata={},
                confidence=conf,
                source_basis=[
                    {
                        "source_type": src_type,
                        "tier": tier,
                        "reference": "fixture",
                    }
                ],
                freshness_state="fresh",
            )
        )
    await session.flush()
    return {label: str(eid) for label, eid in nodes.items()}


async def _cleanup_fixture(session, prefix: str) -> None:
    """Delete every entity under the test URN prefix.
    CASCADE on entity_relationships handles the edges."""
    from sqlalchemy import delete

    from bifrost.storage.models import Entity

    await session.execute(delete(Entity).where(Entity.urn.like(f"urn:test:s02:{prefix}:%")))
    await session.commit()


@pytest.mark.asyncio
async def test_confidence_floor_drops_below_floor_edges(
    fixture_session,
) -> None:
    """``confidence_floor=0.9`` keeps only the high-conf edge."""
    prefix = f"cf-{_uuid.uuid4().hex[:8]}"
    try:
        ids = await _insert_fixture_graph(fixture_session, prefix=prefix)
        await fixture_session.commit()

        sg = await expand_subgraph(
            fixture_session,
            seed_ids=[ids["seed"]],
            depth=1,
            confidence_floor=0.9,
        )
        # Only the seed + the "high" partner survive — "med" (0.70)
        # and "low" (0.40) edges drop below the 0.9 floor.
        node_urns = {n.urn for n in sg.nodes}
        assert f"urn:test:s02:{prefix}:seed" in node_urns
        assert f"urn:test:s02:{prefix}:high" in node_urns
        assert f"urn:test:s02:{prefix}:med" not in node_urns
        assert f"urn:test:s02:{prefix}:low" not in node_urns
    finally:
        await _cleanup_fixture(fixture_session, prefix)


@pytest.mark.asyncio
async def test_direction_upstream_returns_parents_subset(
    fixture_session,
) -> None:
    """With seed in the middle and one parent + one child, the
    ``direction="upstream"`` walk surfaces the parent only."""
    from bifrost.storage.models import Entity, EntityRelationship

    prefix = f"dir-{_uuid.uuid4().hex[:8]}"
    try:
        ids = {
            "seed": _uuid.uuid4(),
            "parent": _uuid.uuid4(),
            "child": _uuid.uuid4(),
        }
        for label, eid in ids.items():
            fixture_session.add(
                Entity(
                    id=eid,
                    entity_type="component",
                    urn=f"urn:test:s02:{prefix}:{label}",
                    name=f"{prefix}-{label}",
                    qualified_name=f"{prefix}.{label}",
                    knowledge_tier=1,
                    entity_metadata={},
                    confidence=0.9,
                    confidence_level="confirmed",
                    source_basis=[],
                    freshness_state="fresh",
                )
            )
        # parent --calls--> seed, seed --calls--> child
        for src, dst in [("parent", "seed"), ("seed", "child")]:
            fixture_session.add(
                EntityRelationship(
                    id=_uuid.uuid4(),
                    source_id=ids[src],
                    target_id=ids[dst],
                    rel_type="calls",
                    rel_metadata={},
                    confidence=0.95,
                    source_basis=[
                        {"source_type": "code_verified", "tier": 1, "reference": "fixture"}
                    ],
                    freshness_state="fresh",
                )
            )
        await fixture_session.commit()

        sg = await expand_subgraph(
            fixture_session,
            seed_ids=[str(ids["seed"])],
            depth=1,
            direction="upstream",
        )
        urns = {n.urn for n in sg.nodes}
        # Upstream should include parent (caller of seed) + seed.
        assert f"urn:test:s02:{prefix}:parent" in urns
        assert f"urn:test:s02:{prefix}:seed" in urns
        # Should NOT include the downstream child (seed calls it).
        assert f"urn:test:s02:{prefix}:child" not in urns
    finally:
        await _cleanup_fixture(fixture_session, prefix)


@pytest.mark.asyncio
async def test_soft_cap_warns_but_does_not_truncate(
    fixture_session,
) -> None:
    """30 candidates with ``soft_cap=10`` returns all 30 +
    ``soft_cap_exceeded=True``."""
    from bifrost.storage.models import Entity, EntityRelationship

    prefix = f"sc-{_uuid.uuid4().hex[:8]}"
    try:
        seed_id = _uuid.uuid4()
        fixture_session.add(
            Entity(
                id=seed_id,
                entity_type="component",
                urn=f"urn:test:s02:{prefix}:seed",
                name=f"{prefix}-seed",
                qualified_name=f"{prefix}.seed",
                knowledge_tier=1,
                entity_metadata={},
                confidence=0.9,
                confidence_level="confirmed",
                source_basis=[],
                freshness_state="fresh",
            )
        )
        # 30 partners, each connected to seed by a high-conf edge.
        for i in range(30):
            pid = _uuid.uuid4()
            fixture_session.add(
                Entity(
                    id=pid,
                    entity_type="component",
                    urn=f"urn:test:s02:{prefix}:p{i}",
                    name=f"{prefix}-p{i}",
                    qualified_name=f"{prefix}.p{i}",
                    knowledge_tier=1,
                    entity_metadata={},
                    confidence=0.9,
                    confidence_level="confirmed",
                    source_basis=[],
                    freshness_state="fresh",
                )
            )
            fixture_session.add(
                EntityRelationship(
                    id=_uuid.uuid4(),
                    source_id=seed_id,
                    target_id=pid,
                    rel_type="calls",
                    rel_metadata={},
                    confidence=0.95,
                    source_basis=[
                        {"source_type": "code_verified", "tier": 1, "reference": "fixture"}
                    ],
                    freshness_state="fresh",
                )
            )
        await fixture_session.commit()

        sg = await expand_subgraph(
            fixture_session,
            seed_ids=[str(seed_id)],
            depth=1,
            soft_cap=10,
        )
        # 1 seed + 30 partners — soft_cap was 10, but no truncation:
        # all 31 nodes returned and the warning flag is set.
        assert sg.stats["total_nodes"] == 31
        assert sg.soft_cap_exceeded is True
    finally:
        await _cleanup_fixture(fixture_session, prefix)
