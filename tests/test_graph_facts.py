"""Tests for the chat-research read path: ``graph_facts``.

Covers:
1. ``graph_facts_for_subject`` returns edges by exact subject URN.
2. Predicate filtering works (and empty filter returns all).
3. ``graph_facts_for_repo`` joins on scope_repo_id correctly.
4. ``min_confidence`` floor drops noisy edges.
5. ``render_facts_block`` produces the expected directive prompt block.
6. Empty results yield ``""`` rather than a stub block.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import delete, or_, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bifrost.research.graph_facts import (
    KIND_PREDICATES,
    graph_facts_for_repo,
    graph_facts_for_subject,
    render_facts_block,
    render_repo_facts_block_for_kind,
)
from bifrost.storage.models import Base, Entity, EntityRelationship, Repo

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://bifrost:bifrost_dev@localhost:5432/bifrost_test",
)


@pytest.fixture
async def factory():
    """Per-test fresh engine + factory bound to the running event loop."""
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(eng, expire_on_commit=False)
    yield sm
    async with sm() as session:
        seeded = select(Entity.id).where(Entity.urn.like("urn:bifrost:repo/AcmeApp-%"))
        await session.execute(
            delete(EntityRelationship).where(
                or_(
                    EntityRelationship.source_id.in_(seeded),
                    EntityRelationship.target_id.in_(seeded),
                )
            )
        )
        await session.execute(delete(Entity).where(Entity.urn.like("urn:bifrost:repo/AcmeApp-%")))
        await session.execute(delete(Repo).where(Repo.urn.like("urn:bifrost:repo/AcmeApp-%")))
        await session.commit()
    await eng.dispose()


async def _seed(
    factory,
    *,
    repo_name: str = "AcmeApp",
    component_name: str = "OrderRouter",
    target_name: str = "TokenService",
    rel_type: str = "calls_service",
    confidence: float = 0.9,
) -> tuple[uuid.UUID, str, str]:
    """Insert a repo + 2 entities + 1 edge.

    Returns (repo_id, source_urn, target_urn). Each call uses unique
    UUID-suffixed names so tests don't collide on the unique-URN
    constraint when run in the same session-scoped DB.
    """
    suffix = uuid.uuid4().hex[:8]
    repo_full = f"{repo_name}-{suffix}"
    src_full = f"{component_name}-{suffix}"
    tgt_full = f"{target_name}-{suffix}"
    src_urn = f"urn:bifrost:repo/{repo_full}/component/{src_full}"
    tgt_urn = f"urn:bifrost:repo/{repo_full}/component/{tgt_full}"

    async with factory() as session:
        repo = Repo(
            id=uuid.uuid4(),
            urn=f"urn:bifrost:repo/{repo_full}",
            name=repo_full,
            url=f"https://github.com/test/{repo_full}",
        )
        session.add(repo)
        await session.flush()

        src = Entity(
            id=uuid.uuid4(),
            entity_type="component",
            urn=src_urn,
            name=src_full,
            scope_repo_id=repo.id,
            confidence=0.9,
            confidence_level="confirmed",
            knowledge_tier=1,
        )
        tgt = Entity(
            id=uuid.uuid4(),
            entity_type="service",
            urn=tgt_urn,
            name=tgt_full,
            confidence=0.9,
            confidence_level="confirmed",
            knowledge_tier=1,
        )
        session.add(src)
        session.add(tgt)
        await session.flush()

        rel = EntityRelationship(
            id=uuid.uuid4(),
            source_id=src.id,
            target_id=tgt.id,
            rel_type=rel_type,
            confidence=confidence,
            freshness_state="fresh",
            source_basis=[],
        )
        session.add(rel)
        await session.commit()

        return repo.id, src_urn, tgt_urn


@pytest.mark.asyncio
async def test_graph_facts_for_subject_returns_edge(factory) -> None:
    _, src_urn, tgt_urn = await _seed(factory)
    edges = await graph_facts_for_subject(
        session_factory=factory,
        subject_urn=src_urn,
    )
    assert len(edges) == 1
    e = edges[0]
    assert e.subject_urn == src_urn
    assert e.target_urn == tgt_urn
    assert e.rel_type == "calls_service"
    assert e.confidence == 0.9


@pytest.mark.asyncio
async def test_graph_facts_predicate_filter(factory) -> None:
    _, src_urn, _ = await _seed(factory, rel_type="calls_service")

    matched = await graph_facts_for_subject(
        session_factory=factory,
        subject_urn=src_urn,
        predicates=["calls_service"],
    )
    assert len(matched) == 1

    skipped = await graph_facts_for_subject(
        session_factory=factory,
        subject_urn=src_urn,
        predicates=["has_jira_ticket"],
    )
    assert skipped == []


@pytest.mark.asyncio
async def test_graph_facts_min_confidence_floor(factory) -> None:
    _, src_urn, _ = await _seed(factory, confidence=0.4)

    above_floor = await graph_facts_for_subject(
        session_factory=factory,
        subject_urn=src_urn,
        min_confidence=0.3,
    )
    assert len(above_floor) == 1

    below_floor = await graph_facts_for_subject(
        session_factory=factory,
        subject_urn=src_urn,
        min_confidence=0.5,
    )
    assert below_floor == []


@pytest.mark.asyncio
async def test_graph_facts_for_repo_joins_scope_repo_id(factory) -> None:
    repo_id, src_urn, _ = await _seed(factory)
    edges = await graph_facts_for_repo(
        session_factory=factory,
        repo_id=repo_id,
    )
    assert len(edges) == 1
    # The repo-scoped variant prefixes "name [urn]" in subject_urn
    assert src_urn in edges[0].subject_urn


@pytest.mark.asyncio
async def test_render_repo_facts_block_jira_filter(factory) -> None:
    """The jira agent's predicate filter should hide a calls_service
    edge — the block returns empty, the agent's prompt is unchanged.
    """
    repo_id, _, _ = await _seed(factory, rel_type="calls_service")
    block = await render_repo_facts_block_for_kind(
        session_factory=factory,
        repo_id=repo_id,
        repo_name="AcmeApp",
        kind="jira",
    )
    assert block == ""


@pytest.mark.asyncio
async def test_render_repo_facts_block_code_view_includes_edge(factory) -> None:
    """The code agent gets the union view (no predicate filter), so it
    should see calls_service and the rendered block contains the edge.
    """
    repo_id, _, _ = await _seed(factory, rel_type="calls_service")
    block = await render_repo_facts_block_for_kind(
        session_factory=factory,
        repo_id=repo_id,
        repo_name="AcmeApp",
        kind="code",
    )
    assert "Known facts about AcmeApp" in block
    assert "calls_service" in block
    assert "DO NOT re-research" in block


def test_render_facts_block_empty_returns_empty_string() -> None:
    """No edges → no block. Caller can splice unconditionally."""
    assert render_facts_block([]) == ""


def test_kind_predicates_cover_all_per_source_kinds() -> None:
    """Sanity: every per-source agent kind has an entry, including the
    catch-all ``code`` kind (whose value is ``None``).
    """
    expected = {"code", "jira", "confluence", "github", "slack", "gdocs"}
    assert set(KIND_PREDICATES.keys()) == expected
    assert KIND_PREDICATES["code"] is None
    # Non-code kinds have a non-empty predicate list
    for k in expected - {"code"}:
        assert KIND_PREDICATES[k]
