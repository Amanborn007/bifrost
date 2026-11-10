"""subgraph_hydrate — round-trip prior URN list to hydrated EntitySnippets.

Integration tests against real Postgres. Inserts a handful of entities
+ relationships into the ``db_session`` fixture's DB, then hydrates by
URN list and asserts the shape, edges, repo-grouping, and bounds.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.research.subgraph_hydrate import (
    HydratedSubgraph,
    hydrate_prior_subgraph,
    hydrated_summary,
)
from bifrost.storage.models import Entity, EntityRelationship, Repo


@pytest.fixture
def session_factory_for(engine):
    """Wrap the session_scoped engine in an async_sessionmaker the
    hydrator can use as `session_factory()`."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def _make_repo(
    session: AsyncSession,
    *,
    name: str,
    urn: str | None = None,
) -> Repo:
    repo = Repo(
        id=uuid.uuid4(),
        urn=urn or f"urn:bifrost:repo:{name}",
        name=name,
        url=f"https://github.com/test/{name}",
    )
    session.add(repo)
    await session.flush()
    return repo


async def _make_entity(
    session: AsyncSession,
    *,
    urn: str,
    name: str,
    repo: Repo | None = None,
    entity_type: str = "component",
    purpose: str = "",
    source_file: str | None = None,
) -> Entity:
    metadata: dict = {}
    if purpose:
        metadata["purpose"] = purpose
    if source_file:
        metadata["source_file"] = source_file
    e = Entity(
        id=uuid.uuid4(),
        urn=urn,
        name=name,
        entity_type=entity_type,
        scope_repo_id=repo.id if repo else None,
        knowledge_tier=1,
        entity_metadata=metadata,
    )
    session.add(e)
    await session.flush()
    return e


async def _make_relationship(
    session: AsyncSession,
    *,
    source: Entity,
    target: Entity,
    rel_type: str,
) -> EntityRelationship:
    r = EntityRelationship(
        id=uuid.uuid4(),
        source_id=source.id,
        target_id=target.id,
        rel_type=rel_type,
    )
    session.add(r)
    await session.flush()
    return r


@pytest.mark.asyncio
async def test_returns_empty_when_no_urns(session_factory_for) -> None:
    out = await hydrate_prior_subgraph(
        urns=[],
        session_factory=session_factory_for,
    )
    assert isinstance(out, HydratedSubgraph)
    assert out.is_empty()
    assert out.repos == []
    assert out.total_entities == 0
    assert out.total_edges == 0


@pytest.mark.asyncio
async def test_returns_empty_when_all_urns_missing(
    session_factory_for,
    db_session,
) -> None:
    # No entities exist at these URNs.
    out = await hydrate_prior_subgraph(
        urns=["urn:bifrost:repo:does-not-exist/component/X"],
        session_factory=session_factory_for,
    )
    assert out.is_empty()
    assert "urn:bifrost:repo:does-not-exist/component/X" in out.missing_urns


@pytest.mark.asyncio
async def test_hydrates_single_repo_entities_with_edges(
    session_factory_for,
    db_session,
) -> None:
    repo = await _make_repo(db_session, name="orders-h1")
    e1 = await _make_entity(
        db_session,
        repo=repo,
        urn="urn:test:h1:RetryQueue",
        name="RetryQueue",
        entity_type="component",
        purpose="Exponential-backoff queue for retried delivery attempts",
        source_file="src/retry/queue.py",
    )
    e2 = await _make_entity(
        db_session,
        repo=repo,
        urn="urn:test:h1:GatewayClient",
        name="GatewayClient",
        entity_type="component",
        purpose="Carrier-gateway HTTP client",
    )
    e3 = await _make_entity(
        db_session,
        repo=repo,
        urn="urn:test:h1:UnrelatedLegacy",
        name="UnrelatedLegacy",
        entity_type="component",
    )
    await _make_relationship(db_session, source=e1, target=e2, rel_type="calls")
    await _make_relationship(db_session, source=e3, target=e1, rel_type="depends_on")
    await db_session.commit()

    out = await hydrate_prior_subgraph(
        urns=[e1.urn, e2.urn],  # NOT including e3
        session_factory=session_factory_for,
    )

    assert not out.is_empty()
    assert len(out.repos) == 1
    sub = out.repos[0]
    assert sub.repo_name == "orders-h1"
    fetched_urns = {s.urn for s in sub.entities}
    assert fetched_urns == {e1.urn, e2.urn}
    # _attach_graph_edges resolves the e1->e2 calls edge and the
    # e3->e1 depends_on edge (e3 isn't in our snippet set, but the
    # helper still creates an inbound edge on e1 whose partner is e3
    # since e3 exists in the partner_meta lookup).
    e1_snippet = next(s for s in sub.entities if s.urn == e1.urn)
    out_rels = {(edge.rel_type, edge.other_urn) for edge in e1_snippet.outbound}
    in_rels = {(edge.rel_type, edge.other_urn) for edge in e1_snippet.inbound}
    assert ("calls", e2.urn) in out_rels
    assert ("depends_on", e3.urn) in in_rels
    assert out.total_entities == 2


@pytest.mark.asyncio
async def test_groups_by_repo_and_orders_by_size(
    session_factory_for,
    db_session,
) -> None:
    big = await _make_repo(db_session, name="big-repo")
    small = await _make_repo(db_session, name="small-repo")

    big_urns = []
    for i in range(3):
        e = await _make_entity(
            db_session,
            repo=big,
            urn=f"urn:test:h2:big/{i}",
            name=f"BigC{i}",
        )
        big_urns.append(e.urn)
    small_urns = []
    for i in range(1):
        e = await _make_entity(
            db_session,
            repo=small,
            urn=f"urn:test:h2:small/{i}",
            name=f"SmallC{i}",
        )
        small_urns.append(e.urn)
    await db_session.commit()

    out = await hydrate_prior_subgraph(
        urns=big_urns + small_urns,
        session_factory=session_factory_for,
    )

    assert [r.repo_name for r in out.repos] == ["big-repo", "small-repo"]
    assert out.repos[0].entities and len(out.repos[0].entities) == 3
    assert len(out.repos[1].entities) == 1
    assert out.total_entities == 4


@pytest.mark.asyncio
async def test_caps_entities_per_repo(
    session_factory_for,
    db_session,
) -> None:
    repo = await _make_repo(db_session, name="bulky-repo")
    urns = []
    for i in range(50):
        e = await _make_entity(
            db_session,
            repo=repo,
            urn=f"urn:test:h3:bulky/{i}",
            name=f"E{i}",
        )
        urns.append(e.urn)
    await db_session.commit()

    out = await hydrate_prior_subgraph(
        urns=urns,
        session_factory=session_factory_for,
        max_entities_per_repo=10,
    )

    assert len(out.repos) == 1
    assert len(out.repos[0].entities) == 10
    assert out.total_entities == 10


@pytest.mark.asyncio
async def test_caps_total_repos(
    session_factory_for,
    db_session,
) -> None:
    urns: list[str] = []
    for r_idx in range(8):  # 8 repos
        repo = await _make_repo(db_session, name=f"r-{r_idx}")
        for e_idx in range(2):  # 2 entities each
            e = await _make_entity(
                db_session,
                repo=repo,
                urn=f"urn:test:h4:{r_idx}/{e_idx}",
                name=f"r{r_idx}e{e_idx}",
            )
            urns.append(e.urn)
    await db_session.commit()

    out = await hydrate_prior_subgraph(
        urns=urns,
        session_factory=session_factory_for,
        max_repos=3,
    )

    assert len(out.repos) == 3
    assert out.total_entities == 6  # 3 repos × 2 entities


@pytest.mark.asyncio
async def test_partial_hit_records_missing_urns(
    session_factory_for,
    db_session,
) -> None:
    repo = await _make_repo(db_session, name="partial-h5")
    e = await _make_entity(
        db_session,
        repo=repo,
        urn="urn:test:h5:Real",
        name="Real",
    )
    await db_session.commit()

    out = await hydrate_prior_subgraph(
        urns=[e.urn, "urn:test:h5:Ghost", "urn:test:h5:Vanished"],
        session_factory=session_factory_for,
    )

    assert out.total_entities == 1
    assert sorted(out.missing_urns) == [
        "urn:test:h5:Ghost",
        "urn:test:h5:Vanished",
    ]


@pytest.mark.asyncio
async def test_dedups_input_urns(
    session_factory_for,
    db_session,
) -> None:
    repo = await _make_repo(db_session, name="dedup-h6")
    e = await _make_entity(
        db_session,
        repo=repo,
        urn="urn:test:h6:Once",
        name="Once",
    )
    await db_session.commit()

    out = await hydrate_prior_subgraph(
        urns=[e.urn, e.urn, e.urn],
        session_factory=session_factory_for,
    )

    assert out.total_entities == 1


@pytest.mark.asyncio
async def test_summary_helper_shape(
    session_factory_for,
    db_session,
) -> None:
    repo = await _make_repo(db_session, name="summary-h7")
    e1 = await _make_entity(
        db_session,
        repo=repo,
        urn="urn:test:h7:A",
        name="A",
    )
    e2 = await _make_entity(
        db_session,
        repo=repo,
        urn="urn:test:h7:B",
        name="B",
    )
    await _make_relationship(db_session, source=e1, target=e2, rel_type="calls")
    await db_session.commit()

    out = await hydrate_prior_subgraph(
        urns=[e1.urn, e2.urn, "urn:test:h7:Missing"],
        session_factory=session_factory_for,
    )

    summary = hydrated_summary(out)
    assert summary["total_entities"] == 2
    assert summary["total_edges"] == 1
    assert summary["repo_count"] == 1
    assert summary["repo_names"] == ["summary-h7"]
    assert summary["missing_urns"] == 1
