"""Tests for the v2 semantic + TTL + commit-bound agent cache.

Covers the three behaviours that the cross-run cache must enforce:
  1. Semantic match: paraphrased questions hit the same cached row
     (cosine similarity ≥ threshold).
  2. Per-kind TTL: rows older than the kind's TTL are not returned.
  3. Code-agent commit binding: rows for a different commit_sha are
     not returned even if everything else matches.
  4. Saved-cost crediting: a hit increments
     ``research_runs.saved_cost_usd`` with the source row's cost.

These tests insert ``agent_results`` rows directly with controlled
embeddings so we can drive the lookup deterministically without
spinning up the embedding model.
"""

from __future__ import annotations

import math
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bifrost.research.agent_cache import (
    _KIND_TTL,
    _settings_threshold,
    cached_agent,
    cost_from_usage,
)
from bifrost.research.runs import start_run
from bifrost.storage.models import AgentResult
from bifrost.storage.repository import EntityRepository


@pytest.fixture
def session_factory_for(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _make_conversation_and_run(
    factory: async_sessionmaker,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with factory() as session:
        repo = EntityRepository(session)
        conv = await repo.create_conversation(
            session_id="cache-sem-session",
            title="semantic cache test",
            scope_repo_ids=[],
        )
        await session.commit()
        cid = conv.id
    run_id = await start_run(factory, conversation_id=cid, turn_id=1)
    return cid, run_id


@dataclass
class _FakeReport:
    target: str
    text: str
    cost: float = 0.0

    def to_dict(self) -> dict:
        return {"target": self.target, "text": self.text, "cost": self.cost}

    @classmethod
    def from_dict(cls, d: dict) -> _FakeReport:
        return cls(target=d["target"], text=d["text"], cost=float(d.get("cost", 0)))


def _normalised(vec: list[float]) -> list[float]:
    """Return ``vec`` normalised to unit length so cosine similarity
    against another unit-length vector equals their dot product —
    makes the similarity threshold easy to reason about."""
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def _embed(seed: int, dim: int = 384) -> list[float]:
    """Produce a deterministic 384-dim embedding from ``seed``. Two
    embeddings with the same low ``(seed % 8)`` are nearly identical
    (cosine ~1.0); embeddings with different ``seed % 8`` are
    near-orthogonal."""
    base = [0.0] * dim
    cluster = seed % 8
    for i in range(dim):
        if i % 8 == cluster:
            base[i] = 1.0 + (seed * 1e-6)  # tiny per-seed jitter
    return _normalised(base)


async def _insert_row(
    factory: async_sessionmaker,
    *,
    conversation_id: uuid.UUID,
    research_run_id: uuid.UUID,
    turn_id: int = 1,
    agent_kind: str = "jira",
    target_id: str = "r1",
    shard_index: int = 0,
    output_json: dict | None = None,
    cost_usd: float = 0.50,
    embedding: list[float] | None = None,
    repo_commit_sha: str | None = None,
    ttl_seconds: int | None = None,
    created_at: datetime | None = None,
) -> uuid.UUID:
    async with factory() as session:
        ar = AgentResult(
            research_run_id=research_run_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            agent_kind=agent_kind,
            target_id=target_id,
            shard_index=shard_index,
            scope_hash="legacy",
            status="success",
            output_json=output_json or {"target": target_id, "text": "seed", "cost": cost_usd},
            cost_usd=cost_usd,
            duration_ms=1234,
            question_embedding=embedding,
            repo_commit_sha=repo_commit_sha,
            ttl_seconds=(
                ttl_seconds if ttl_seconds is not None else _KIND_TTL.get(agent_kind, 3600)
            ),
            was_cache_hit=False,
            source_run_id=None,
        )
        session.add(ar)
        await session.flush()
        rid = ar.id
        if created_at is not None:
            await session.execute(
                text("UPDATE agent_results SET created_at = :ts WHERE id = :id"),
                {"ts": created_at, "id": rid},
            )
        await session.commit()
        return rid


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_semantic_hit_on_paraphrase(session_factory_for) -> None:
    """A pre-existing row whose embedding is ~identical to the new
    query's embedding should hit Stage 2 — runner is NOT called.

    Each test uses a unique ``target_id`` so the session-scoped DB
    fixture (rows persist across tests) doesn't bleed state."""
    cid_a, run_a = await _make_conversation_and_run(session_factory_for)
    cid_b, run_b = await _make_conversation_and_run(session_factory_for)

    target = "test-paraphrase-orders"

    await _insert_row(
        session_factory_for,
        conversation_id=cid_a,
        research_run_id=run_a,
        target_id=target,
        agent_kind="jira",
        cost_usd=0.42,
        embedding=_embed(seed=3),
        output_json={"target": target, "text": "from prior run", "cost": 0.42},
    )

    runner_called = {"n": 0}

    async def _runner() -> _FakeReport:
        runner_called["n"] += 1
        return _FakeReport(target=target, text="should not appear")

    report, hit = await cached_agent(
        session_factory=session_factory_for,
        research_run_id=run_b,
        conversation_id=cid_b,
        turn_id=1,
        agent_kind="jira",
        target_id=target,
        shard_index=0,
        scope_hash="x",
        runner=_runner,
        serialize=lambda r: r.to_dict(),
        deserialize=_FakeReport.from_dict,
        question_embedding=_embed(seed=3),  # near-identical
    )
    assert hit == "semantic"
    assert report.text == "from prior run"
    assert runner_called["n"] == 0

    # saved_cost_usd should now reflect the credited amount.
    async with session_factory_for() as session:
        rr = (
            await session.execute(
                text("SELECT saved_cost_usd FROM research_runs WHERE id = :id"),
                {"id": run_b},
            )
        ).scalar_one()
    assert float(rr) == pytest.approx(0.42, abs=1e-6)


@pytest.mark.asyncio
async def test_semantic_miss_on_dissimilar(session_factory_for) -> None:
    """Different question embeddings → no Stage 2 hit, runner runs."""
    cid_a, run_a = await _make_conversation_and_run(session_factory_for)
    cid_b, run_b = await _make_conversation_and_run(session_factory_for)
    target = "test-dissimilar-orders"

    await _insert_row(
        session_factory_for,
        conversation_id=cid_a,
        research_run_id=run_a,
        target_id=target,
        agent_kind="jira",
        embedding=_embed(seed=3),
    )

    runner_called = {"n": 0}

    async def _runner() -> _FakeReport:
        runner_called["n"] += 1
        return _FakeReport(target=target, text="fresh")

    report, hit = await cached_agent(
        session_factory=session_factory_for,
        research_run_id=run_b,
        conversation_id=cid_b,
        turn_id=1,
        agent_kind="jira",
        target_id=target,
        shard_index=0,
        scope_hash="x",
        runner=_runner,
        serialize=lambda r: r.to_dict(),
        deserialize=_FakeReport.from_dict,
        question_embedding=_embed(seed=4),  # different cluster, ~orthogonal
    )
    assert hit == "miss"
    assert report.text == "fresh"
    assert runner_called["n"] == 1


@pytest.mark.asyncio
async def test_failed_report_not_cached_and_reruns(session_factory_for) -> None:
    """Cache-poisoning guard: a Stage-3 fresh run whose report is a
    FAILURE (``is_cacheable`` returns False) must NOT persist a row, and
    a SECOND call must re-invoke the runner instead of being served a
    poisoned cache hit.

    This is the core fix: a transient code-agent failure must not become
    a permanent "not code-verified" answer replayed for every paraphrase.
    """
    cid, run = await _make_conversation_and_run(session_factory_for)
    target = "test-poison-guard-fail"

    runner_called = {"n": 0}

    async def _runner() -> _FakeReport:
        runner_called["n"] += 1
        # Marker text simulates a failed report (relevance="none", error=...)
        return _FakeReport(target=target, text="FAILED: transient claude -p error")

    def _is_cacheable(r: _FakeReport) -> bool:
        return not r.text.startswith("FAILED")

    # First call — Stage 3 miss, report is a failure → not persisted.
    report1, hit1 = await cached_agent(
        session_factory=session_factory_for,
        research_run_id=run,
        conversation_id=cid,
        turn_id=1,
        agent_kind="repo",
        target_id=target,
        shard_index=0,
        scope_hash="x",
        runner=_runner,
        serialize=lambda r: r.to_dict(),
        deserialize=_FakeReport.from_dict,
        question_embedding=_embed(seed=3),
        repo_commit_sha="deadbee",
        is_cacheable=_is_cacheable,
    )
    assert hit1 == "miss"
    assert report1.text.startswith("FAILED")
    assert runner_called["n"] == 1

    # No row should have been persisted for this target/turn.
    async with session_factory_for() as session:
        cnt = (
            await session.execute(
                text(
                    "SELECT count(*) FROM agent_results "
                    "WHERE target_id = :t AND conversation_id = :c AND turn_id = 1"
                ),
                {"t": target, "c": cid},
            )
        ).scalar_one()
    assert int(cnt) == 0

    # Second call — clean miss again, runner re-invoked (NOT served stale).
    report2, hit2 = await cached_agent(
        session_factory=session_factory_for,
        research_run_id=run,
        conversation_id=cid,
        turn_id=1,
        agent_kind="repo",
        target_id=target,
        shard_index=0,
        scope_hash="x",
        runner=_runner,
        serialize=lambda r: r.to_dict(),
        deserialize=_FakeReport.from_dict,
        question_embedding=_embed(seed=3),
        repo_commit_sha="deadbee",
        is_cacheable=_is_cacheable,
    )
    assert hit2 == "miss"
    assert runner_called["n"] == 2


@pytest.mark.asyncio
async def test_successful_report_cached_and_hits(session_factory_for) -> None:
    """When ``is_cacheable`` returns True the fresh report IS persisted,
    and a second (exact-turn) call is served from cache — the runner is
    called exactly once."""
    cid, run = await _make_conversation_and_run(session_factory_for)
    target = "test-poison-guard-ok"

    runner_called = {"n": 0}

    async def _runner() -> _FakeReport:
        runner_called["n"] += 1
        return _FakeReport(target=target, text="real verified answer")

    # First call — Stage 3 miss, report is good → persisted.
    report1, hit1 = await cached_agent(
        session_factory=session_factory_for,
        research_run_id=run,
        conversation_id=cid,
        turn_id=1,
        agent_kind="repo",
        target_id=target,
        shard_index=0,
        scope_hash="x",
        runner=_runner,
        serialize=lambda r: r.to_dict(),
        deserialize=_FakeReport.from_dict,
        question_embedding=_embed(seed=3),
        repo_commit_sha="cafef00",
        is_cacheable=lambda r: True,
    )
    assert hit1 == "miss"
    assert runner_called["n"] == 1

    # Exactly one row persisted for this target/turn.
    async with session_factory_for() as session:
        cnt = (
            await session.execute(
                text(
                    "SELECT count(*) FROM agent_results "
                    "WHERE target_id = :t AND conversation_id = :c AND turn_id = 1"
                ),
                {"t": target, "c": cid},
            )
        ).scalar_one()
    assert int(cnt) == 1

    # Second call — exact-turn (Stage 1) hit; runner NOT re-invoked.
    report2, hit2 = await cached_agent(
        session_factory=session_factory_for,
        research_run_id=run,
        conversation_id=cid,
        turn_id=1,
        agent_kind="repo",
        target_id=target,
        shard_index=0,
        scope_hash="x",
        runner=_runner,
        serialize=lambda r: r.to_dict(),
        deserialize=_FakeReport.from_dict,
        question_embedding=_embed(seed=3),
        repo_commit_sha="cafef00",
        is_cacheable=lambda r: True,
    )
    assert hit2 == "exact"
    assert report2.text == "real verified answer"
    assert runner_called["n"] == 1


@pytest.mark.asyncio
async def test_old_rows_still_returned_with_freshness_metadata(
    session_factory_for,
) -> None:
    """Bifrost is a knowledge BASE, not a TTL cache.
    A 2-month-old jira row is still returned by semantic lookup.
    Stale data is information; the agent decides whether to re-verify.
    The runner is NOT called when a semantically-matching old row
    exists, regardless of ``ttl_seconds`` or ``created_at``.
    """
    cid_a, run_a = await _make_conversation_and_run(session_factory_for)
    cid_b, run_b = await _make_conversation_and_run(session_factory_for)
    target = "test-permanent-orders"

    # Backdate the row 2 hours — would have been TTL-evicted under the
    # old semantics (jira TTL was 1h). Now: still reachable.
    await _insert_row(
        session_factory_for,
        conversation_id=cid_a,
        research_run_id=run_a,
        target_id=target,
        agent_kind="jira",
        embedding=_embed(seed=3),
        created_at=datetime.now(UTC) - timedelta(hours=2),
        ttl_seconds=3600,
        cost_usd=0.42,
        output_json={"target": target, "text": "from prior run", "cost": 0.42},
    )

    runner_called = {"n": 0}

    async def _runner() -> _FakeReport:
        runner_called["n"] += 1
        return _FakeReport(target=target, text="should not appear")

    report, hit = await cached_agent(
        session_factory=session_factory_for,
        research_run_id=run_b,
        conversation_id=cid_b,
        turn_id=1,
        agent_kind="jira",
        target_id=target,
        shard_index=0,
        scope_hash="x",
        runner=_runner,
        serialize=lambda r: r.to_dict(),
        deserialize=_FakeReport.from_dict,
        question_embedding=_embed(seed=3),
    )
    # Knowledge base hit, not a miss — old rows still serve.
    assert hit == "semantic"
    assert runner_called["n"] == 0
    assert report.text == "from prior run"


@pytest.mark.asyncio
async def test_repo_commit_invalidation(session_factory_for) -> None:
    """Code-agent rows are bound to ``repo_commit_sha``. A query
    against a NEW commit must not match the OLD-commit row."""
    cid_a, run_a = await _make_conversation_and_run(session_factory_for)
    cid_b, run_b = await _make_conversation_and_run(session_factory_for)
    target = "test-commit-invalidate"

    await _insert_row(
        session_factory_for,
        conversation_id=cid_a,
        research_run_id=run_a,
        target_id=target,
        agent_kind="repo",
        embedding=_embed(seed=3),
        repo_commit_sha="aaaaaaa",
    )

    async def _runner() -> _FakeReport:
        return _FakeReport(target=target, text="fresh")

    # Same target + similar embedding, different commit sha → miss.
    _, hit = await cached_agent(
        session_factory=session_factory_for,
        research_run_id=run_b,
        conversation_id=cid_b,
        turn_id=1,
        agent_kind="repo",
        target_id=target,
        shard_index=0,
        scope_hash="x",
        runner=_runner,
        serialize=lambda r: r.to_dict(),
        deserialize=_FakeReport.from_dict,
        question_embedding=_embed(seed=3),
        repo_commit_sha="bbbbbbb",
    )
    assert hit == "miss"


@pytest.mark.asyncio
async def test_repo_commit_match_hits(session_factory_for) -> None:
    """Same commit sha → semantic hit fires."""
    cid_a, run_a = await _make_conversation_and_run(session_factory_for)
    cid_b, run_b = await _make_conversation_and_run(session_factory_for)
    target = "test-commit-match"

    await _insert_row(
        session_factory_for,
        conversation_id=cid_a,
        research_run_id=run_a,
        target_id=target,
        agent_kind="repo",
        cost_usd=2.10,
        embedding=_embed(seed=3),
        repo_commit_sha="cccccc",
        output_json={"target": target, "text": "from older turn", "cost": 2.10},
    )

    runner_called = {"n": 0}

    async def _runner() -> _FakeReport:
        runner_called["n"] += 1
        return _FakeReport(target=target, text="should not run")

    report, hit = await cached_agent(
        session_factory=session_factory_for,
        research_run_id=run_b,
        conversation_id=cid_b,
        turn_id=1,
        agent_kind="repo",
        target_id=target,
        shard_index=0,
        scope_hash="x",
        runner=_runner,
        serialize=lambda r: r.to_dict(),
        deserialize=_FakeReport.from_dict,
        question_embedding=_embed(seed=3),
        repo_commit_sha="cccccc",
    )
    assert hit == "semantic"
    assert runner_called["n"] == 0
    assert report.text == "from older turn"


@pytest.mark.asyncio
async def test_threshold_setting() -> None:
    """The threshold comes from the validated cache_sim_threshold field.

    The helper reads the cached settings singleton, so a change to
    BIFROST_CACHE_SIM_THRESHOLD only takes effect after reset_settings()
    (the established test pattern). A non-numeric / out-of-range value no
    longer silently falls back to 0.85 — it fails fast at validate_startup;
    that contract is covered in tests/test_config_validation.py.
    """
    from bifrost.cli.config import reset_settings

    prior = os.environ.get("BIFROST_CACHE_SIM_THRESHOLD")
    try:
        os.environ["BIFROST_CACHE_SIM_THRESHOLD"] = "0.9"
        reset_settings()
        assert _settings_threshold() == 0.9
        os.environ.pop("BIFROST_CACHE_SIM_THRESHOLD", None)
        reset_settings()
        assert _settings_threshold() == 0.85  # field default
    finally:
        if prior is None:
            os.environ.pop("BIFROST_CACHE_SIM_THRESHOLD", None)
        else:
            os.environ["BIFROST_CACHE_SIM_THRESHOLD"] = prior
        reset_settings()


def test_cost_from_usage_extractor() -> None:
    @dataclass
    class _Usage:
        total_cost_usd: float = 0.42

    @dataclass
    class _Report:
        usage: _Usage | None

    assert cost_from_usage(_Report(usage=_Usage())) == pytest.approx(0.42)
    assert cost_from_usage(_Report(usage=None)) == 0.0
    assert cost_from_usage(None) == 0.0
    # Dict-based usage (from JSON deserialization)
    assert cost_from_usage(_Report(usage={"total_cost_usd": "0.5"})) == 0.5  # type: ignore[arg-type]
