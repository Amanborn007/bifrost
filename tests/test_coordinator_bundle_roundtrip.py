"""Coordinator-level bundle persistence round-trip.

Exercises ``_persist_research_bundle`` (used at the end of every
successful research turn) and ``_load_prior_bundle`` (used at the
start of every chat turn) against the real Postgres test DB. Proves
that what one chat turn saves is exactly what the next turn loads,
including the embedding, sources, and subgraph URNs.

Distinct from test_repository_research_state.py (which tests just
the SQL CRUD) — this test proves the dataclass ↔ JSONB ↔ dataclass
serialization is faithful and the cache-then-DB load order works.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bifrost.research.bundle_cache import (
    BundleSource,
    ResearchBundle,
    reset_bundle_cache_for_tests,
)
from bifrost.research.coordinator import (
    _load_prior_bundle,
    _persist_research_bundle,
)
from bifrost.storage.repository import EntityRepository


@pytest.fixture
def session_factory_for(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _reset_cache():
    """Drop the in-process bundle cache before each test so the load
    path actually exercises the DB read, not the cache hit."""
    reset_bundle_cache_for_tests()
    yield
    reset_bundle_cache_for_tests()


async def _make_conversation(session_factory: async_sessionmaker) -> uuid.UUID:
    async with session_factory() as session:
        repo = EntityRepository(session)
        conv = await repo.create_conversation(
            session_id="rt-session",
            title="bundle round-trip test",
            scope_repo_ids=[],
        )
        await session.commit()
        return conv.id


@pytest.mark.asyncio
async def test_persist_then_load_via_db(session_factory_for) -> None:
    """Save through the coordinator helper, drop cache, then load
    through the coordinator helper. Result should match save input."""
    cid = await _make_conversation(session_factory_for)
    saved = ResearchBundle(
        conversation_id=str(cid),
        last_question="how does orders retry work?",
        last_answer=(
            "Orders uses an exponential-backoff queue with jitter. "
            "Retries are bounded by attempt count + total elapsed time."
        ),
        confidence="confirmed",
        repos_used=["00000000-0000-0000-0000-000000000001"],
        repos_used_names=["orders"],
        scope_rationale="single repo, deep-indexed",
        subgraph_entity_urns=[
            "urn:bifrost:repo/orders/component/RetryQueue",
            "urn:bifrost:repo/orders/component/GatewayClient",
        ],
        sources=[
            BundleSource(kind="jira", ref="SHIP-1", title="retry bug"),
            BundleSource(kind="confluence", ref="page-9", title="Retry RFC"),
        ],
        last_question_embedding=[0.1, 0.2, 0.3],
        created_at=datetime(2026, 4, 26, 10, 0, 0),
    )

    await _persist_research_bundle(
        session_factory=session_factory_for,
        conversation_id=cid,
        bundle=saved,
    )

    # Drop the in-process cache so the load comes from Postgres.
    reset_bundle_cache_for_tests()

    async with session_factory_for() as session:
        repo = EntityRepository(session)
        loaded = await _load_prior_bundle(repo, cid)

    assert loaded is not None
    assert loaded.conversation_id == str(cid)
    assert loaded.last_question == saved.last_question
    assert loaded.last_answer == saved.last_answer
    assert loaded.confidence == "confirmed"
    assert loaded.repos_used_names == ["orders"]
    assert loaded.subgraph_entity_urns == saved.subgraph_entity_urns
    assert [s.ref for s in loaded.sources] == ["SHIP-1", "page-9"]
    assert [s.kind for s in loaded.sources] == ["jira", "confluence"]
    assert loaded.last_question_embedding == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_load_prefers_cache_over_db(session_factory_for) -> None:
    """If a bundle is in the in-memory cache, the load skips the DB
    and returns the cached object — important for hot follow-ups
    that don't pay the JSONB-deserialize cost on every turn."""
    cid = await _make_conversation(session_factory_for)

    saved = ResearchBundle(
        conversation_id=str(cid),
        last_question="from cache",
        last_answer="a" * 200,
        confidence="inferred",
        repos_used=[],
        repos_used_names=["orders"],
        scope_rationale="x",
        subgraph_entity_urns=[],
        sources=[],
        last_question_embedding=None,
    )

    # Save populates BOTH cache + DB.
    await _persist_research_bundle(
        session_factory=session_factory_for,
        conversation_id=cid,
        bundle=saved,
    )

    # Now corrupt the DB row to prove the load came from cache.
    from sqlalchemy import update

    from bifrost.storage.models import ConversationResearchState

    async with session_factory_for() as session:
        await session.execute(
            update(ConversationResearchState)
            .where(ConversationResearchState.conversation_id == cid)
            .values(bundle={"last_question": "DB ROW DIFFERS"})
        )
        await session.commit()

    # Load — should hit cache and return the original.
    async with session_factory_for() as session:
        repo = EntityRepository(session)
        loaded = await _load_prior_bundle(repo, cid)
    assert loaded is not None
    assert loaded.last_question == "from cache"


@pytest.mark.asyncio
async def test_load_falls_back_to_db_when_cache_cold(
    session_factory_for,
) -> None:
    """After a process restart, only the DB row remains; the load
    must still return a valid ResearchBundle."""
    cid = await _make_conversation(session_factory_for)

    saved = ResearchBundle(
        conversation_id=str(cid),
        last_question="cold-start question",
        last_answer="b" * 200,
        confidence="confirmed",
        repos_used=[],
        repos_used_names=["orders"],
        scope_rationale="x",
        subgraph_entity_urns=["urn:bifrost:repo/orders/component/X"],
        sources=[BundleSource(kind="jira", ref="J-1")],
        last_question_embedding=[0.5],
    )
    await _persist_research_bundle(
        session_factory=session_factory_for,
        conversation_id=cid,
        bundle=saved,
    )

    # Simulate process restart: drop cache only.
    reset_bundle_cache_for_tests()

    async with session_factory_for() as session:
        repo = EntityRepository(session)
        loaded = await _load_prior_bundle(repo, cid)

    assert loaded is not None
    assert loaded.last_question == "cold-start question"
    assert loaded.subgraph_entity_urns == [
        "urn:bifrost:repo/orders/component/X",
    ]
    assert loaded.last_question_embedding == [0.5]


@pytest.mark.asyncio
async def test_load_returns_none_for_unknown_conversation(
    session_factory_for,
) -> None:
    """A brand-new conversation (no prior turn) yields None — the
    planner falls back to NEW_SCOPE classification."""
    fake_id = uuid.UUID("99999999-9999-9999-9999-999999999999")
    async with session_factory_for() as session:
        repo = EntityRepository(session)
        loaded = await _load_prior_bundle(repo, fake_id)
    assert loaded is None


@pytest.mark.asyncio
async def test_persist_overwrites_prior_turn_bundle(
    session_factory_for,
) -> None:
    """Each turn upserts; the load returns the LATEST turn's bundle."""
    cid = await _make_conversation(session_factory_for)
    first = ResearchBundle(
        conversation_id=str(cid),
        last_question="turn 1",
        last_answer="a" * 100,
        confidence="confirmed",
        repos_used=[],
        repos_used_names=["orders"],
        scope_rationale="x",
        subgraph_entity_urns=[],
        sources=[],
    )
    second = ResearchBundle(
        conversation_id=str(cid),
        last_question="turn 2",
        last_answer="b" * 100,
        confidence="inferred",
        repos_used=[],
        repos_used_names=["orders", "auth"],
        scope_rationale="extended",
        subgraph_entity_urns=["urn:bifrost:repo/orders/component/X"],
        sources=[BundleSource(kind="jira", ref="J-2")],
    )
    await _persist_research_bundle(
        session_factory=session_factory_for,
        conversation_id=cid,
        bundle=first,
    )
    await _persist_research_bundle(
        session_factory=session_factory_for,
        conversation_id=cid,
        bundle=second,
    )

    reset_bundle_cache_for_tests()

    async with session_factory_for() as session:
        repo = EntityRepository(session)
        loaded = await _load_prior_bundle(repo, cid)
    assert loaded is not None
    assert loaded.last_question == "turn 2"
    assert loaded.repos_used_names == ["orders", "auth"]
    assert loaded.subgraph_entity_urns == [
        "urn:bifrost:repo/orders/component/X",
    ]
