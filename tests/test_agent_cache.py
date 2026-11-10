"""Tests for the agent_results cache helper.

Covers:
- compute_scope_hash is stable + order-independent on lists
- cached_agent returns the runner's result on cache miss + persists
- cached_agent returns the cached value on hit + does NOT call runner
- research_run_id=None disables caching entirely
- runner errors propagate (cache layer doesn't swallow them)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bifrost.research.agent_cache import (
    cached_agent,
    compute_scope_hash,
    count_cache_hits_for_run,
)
from bifrost.research.runs import start_run
from bifrost.storage.repository import EntityRepository


@pytest.fixture
def session_factory_for(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _make_conversation_and_run(
    factory: async_sessionmaker,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Returns (conversation_id, run_id)."""
    async with factory() as session:
        repo = EntityRepository(session)
        conv = await repo.create_conversation(
            session_id="cache-session",
            title="cache test",
            scope_repo_ids=[],
        )
        await session.commit()
        cid = conv.id
    run_id = await start_run(factory, conversation_id=cid, turn_id=1)
    return cid, run_id


@dataclass
class FakeReport:
    repo_id: str
    text: str

    def to_dict(self) -> dict:
        return {"repo_id": self.repo_id, "text": self.text}

    @classmethod
    def from_dict(cls, d: dict) -> FakeReport:
        return cls(repo_id=d["repo_id"], text=d["text"])


def test_compute_scope_hash_stable_order_independent() -> None:
    h1 = compute_scope_hash(["q", "r1", ["a", "b", "c"]])
    h2 = compute_scope_hash(["q", "r1", ["c", "b", "a"]])
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_compute_scope_hash_changes_on_inputs() -> None:
    h1 = compute_scope_hash(["q", "r1"])
    h2 = compute_scope_hash(["q", "r2"])
    assert h1 != h2


@pytest.mark.asyncio
async def test_cache_miss_runs_and_persists(session_factory_for) -> None:
    cid, run_id = await _make_conversation_and_run(session_factory_for)
    call_count = {"n": 0}

    async def _runner() -> FakeReport:
        call_count["n"] += 1
        return FakeReport(repo_id="r1", text="fresh")

    report, hit_kind = await cached_agent(
        session_factory=session_factory_for,
        research_run_id=run_id,
        conversation_id=cid,
        turn_id=1,
        agent_kind="jira",
        target_id="r1",
        shard_index=0,
        scope_hash="abc123",
        runner=_runner,
        serialize=lambda r: r.to_dict(),
        deserialize=FakeReport.from_dict,
    )

    assert hit_kind == "miss"
    assert report.text == "fresh"
    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_cache_hit_returns_cached_without_running(
    session_factory_for,
) -> None:
    cid, run_id = await _make_conversation_and_run(session_factory_for)

    # First call: persists.
    call_count = {"n": 0}

    async def _runner() -> FakeReport:
        call_count["n"] += 1
        return FakeReport(repo_id="r1", text="first run")

    await cached_agent(
        session_factory=session_factory_for,
        research_run_id=run_id,
        conversation_id=cid,
        turn_id=1,
        agent_kind="jira",
        target_id="r1",
        shard_index=0,
        scope_hash="x",
        runner=_runner,
        serialize=lambda r: r.to_dict(),
        deserialize=FakeReport.from_dict,
    )
    assert call_count["n"] == 1

    # Second call same key: should NOT invoke runner.
    async def _should_not_run() -> FakeReport:
        call_count["n"] += 1
        raise AssertionError("runner should not have been invoked")

    report, hit_kind = await cached_agent(
        session_factory=session_factory_for,
        research_run_id=run_id,
        conversation_id=cid,
        turn_id=1,
        agent_kind="jira",
        target_id="r1",
        shard_index=0,
        scope_hash="x",
        runner=_should_not_run,
        serialize=lambda r: r.to_dict(),
        deserialize=FakeReport.from_dict,
    )
    assert hit_kind == "exact"
    assert report.text == "first run"
    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_cache_disabled_when_run_id_none(session_factory_for) -> None:
    """research_run_id=None bypasses cache entirely — runner runs every time."""
    cid, _ = await _make_conversation_and_run(session_factory_for)
    call_count = {"n": 0}

    async def _runner() -> FakeReport:
        call_count["n"] += 1
        return FakeReport(repo_id="r1", text="no cache")

    await cached_agent(
        session_factory=session_factory_for,
        research_run_id=None,
        conversation_id=cid,
        turn_id=1,
        agent_kind="jira",
        target_id="r1",
        shard_index=0,
        scope_hash="x",
        runner=_runner,
        serialize=lambda r: r.to_dict(),
        deserialize=FakeReport.from_dict,
    )
    await cached_agent(
        session_factory=session_factory_for,
        research_run_id=None,
        conversation_id=cid,
        turn_id=1,
        agent_kind="jira",
        target_id="r1",
        shard_index=0,
        scope_hash="x",
        runner=_runner,
        serialize=lambda r: r.to_dict(),
        deserialize=FakeReport.from_dict,
    )
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_runner_error_propagates(session_factory_for) -> None:
    cid, run_id = await _make_conversation_and_run(session_factory_for)

    async def _broken() -> FakeReport:
        raise RuntimeError("agent crashed")

    with pytest.raises(RuntimeError, match="agent crashed"):
        await cached_agent(
            session_factory=session_factory_for,
            research_run_id=run_id,
            conversation_id=cid,
            turn_id=1,
            agent_kind="jira",
            target_id="r1",
            shard_index=0,
            scope_hash="x",
            runner=_broken,
            serialize=lambda r: r.to_dict(),
            deserialize=FakeReport.from_dict,
        )


@pytest.mark.asyncio
async def test_count_cache_hits_for_run(session_factory_for) -> None:
    """``count_cache_hits_for_run`` counts
    rows where ``was_cache_hit = True`` — i.e. rows that were
    persisted as semantic-hit attribution, not fresh runs. Three
    fresh runs under the same turn produce 0 hits (all
    ``was_cache_hit = False`` misses)."""
    cid, run_id = await _make_conversation_and_run(session_factory_for)

    async def _runner_for(target: str) -> FakeReport:
        return FakeReport(repo_id=target, text=f"data-{target}")

    for target in ("r1", "r2", "r3"):
        await cached_agent(
            session_factory=session_factory_for,
            research_run_id=run_id,
            conversation_id=cid,
            turn_id=1,
            agent_kind="jira",
            target_id=target,
            shard_index=0,
            scope_hash="x",
            runner=lambda t=target: _runner_for(t),  # noqa: B023
            serialize=lambda r: r.to_dict(),
            deserialize=FakeReport.from_dict,
        )

    # All three were fresh misses; no semantic-hit attribution rows.
    n = await count_cache_hits_for_run(session_factory_for, run_id)
    assert n == 0


@pytest.mark.asyncio
async def test_different_targets_dont_collide(session_factory_for) -> None:
    """The cache key includes target_id — two repos under the same
    run/turn/agent should each get their own row."""
    cid, run_id = await _make_conversation_and_run(session_factory_for)

    async def _r1() -> FakeReport:
        return FakeReport(repo_id="r1", text="answer for r1")

    async def _r2() -> FakeReport:
        return FakeReport(repo_id="r2", text="answer for r2")

    rep1, _ = await cached_agent(
        session_factory=session_factory_for,
        research_run_id=run_id,
        conversation_id=cid,
        turn_id=1,
        agent_kind="jira",
        target_id="r1",
        shard_index=0,
        scope_hash="h1",
        runner=_r1,
        serialize=lambda r: r.to_dict(),
        deserialize=FakeReport.from_dict,
    )
    rep2, _ = await cached_agent(
        session_factory=session_factory_for,
        research_run_id=run_id,
        conversation_id=cid,
        turn_id=1,
        agent_kind="jira",
        target_id="r2",
        shard_index=0,
        scope_hash="h2",
        runner=_r2,
        serialize=lambda r: r.to_dict(),
        deserialize=FakeReport.from_dict,
    )
    assert rep1.text == "answer for r1"
    assert rep2.text == "answer for r2"
