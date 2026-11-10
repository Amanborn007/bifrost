"""Per-agent result cache for research runs — two-stage:

  Stage 1: exact-turn match (resume path). Same ``(conversation_id,
           turn_id, agent_kind, target_id, shard_index)`` row from a
           prior interrupted attempt of THIS turn.

  Stage 2: cross-run semantic match. Same ``(agent_kind, target_id,
           shard_index)``, optional ``repo_commit_sha`` equality for
           code agents, ``created_at`` within per-kind TTL, AND
           question-embedding cosine similarity ≥ threshold.

  Stage 3: miss → run the agent fresh, capture its real ``cost_usd``
           from ``Usage.total_cost_usd``, persist with embedding +
           commit-sha + per-kind ``ttl_seconds``.

On a Stage-2 hit we ALSO write a thin row attributed to the current
turn (``was_cache_hit=True``, ``source_run_id=<original run>``) so
the conv's evidence chain stays intact and Stage-1 resumes work
correctly. We credit ``research_runs.saved_cost_usd`` with the
SOURCE row's ``cost_usd`` — the actual money saved.

Tunables:
- ``BIFROST_CACHE_SIM_THRESHOLD`` (env, default 0.85): cosine
  similarity floor for Stage 2 hits. Higher = stricter matching,
  fewer hits but higher trust.
- ``_KIND_TTL`` (this module): per-kind freshness budget. Code is
  bound by commit sha (effectively infinite within same head); Jira
  / GitHub at 1h; Confluence 6h; Slack 12h; external 30m.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid as _uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypeVar

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from bifrost.storage.models import AgentResult, ResearchRun

logger = logging.getLogger(__name__)


AgentKind = Literal[
    "repo",
    "jira",
    "confluence",
    "github",
    "slack",
    "external",
    "gdocs",
]


T = TypeVar("T")


# Per-kind freshness budget. Repo cache is *commit-bound* — a new HEAD
# invalidates all old rows for that repo's code agent, regardless of
# age — so the time-based TTL there is symbolic (1 year). For
# external sources, this is the maximum staleness we accept on a
# semantic-match cache hit before we'd rather just re-fetch.
_KIND_TTL: dict[str, int] = {
    "repo": 365 * 24 * 3600,  # commit-bound; time TTL is a backstop
    "jira": 1 * 3600,  # tickets evolve fast
    "confluence": 6 * 3600,  # docs are slower
    "github": 1 * 3600,  # PRs / issues evolve
    "slack": 12 * 3600,  # messages append-only mostly
    "external": 30 * 60,  # org-wide search; conservative
    "gdocs": 6 * 3600,  # design docs / RFCs evolve slowly, like Confluence
}


def _settings_threshold() -> float:
    """Similarity threshold from the validated config surface (default 0.85).

    A float in [0, 1]; the lookup distance bound is ``1 - threshold``
    (pgvector's ``<=>`` is cosine distance). This reads the bounded
    ``cache_sim_threshold`` field instead of parsing ``BIFROST_CACHE_SIM_THRESHOLD``
    raw — an out-of-range value fails fast at boot rather than silently
    clamping here.
    """
    from bifrost.cli.config import get_settings

    return get_settings().cache_sim_threshold


def compute_scope_hash(parts: list[Any]) -> str:
    """Stable sha256 hex digest over the JSON-serialized parts.

    Kept for backward-compat with existing call sites. The new
    semantic cache uses pgvector embeddings instead, but the
    scope_hash column is still written so legacy tooling that
    queries by it keeps working.
    """

    def _normalize(v: Any) -> Any:
        if isinstance(v, (list, tuple)):
            return [_normalize(x) for x in sorted(v, key=lambda x: str(x))]
        if isinstance(v, set):
            return sorted(str(x) for x in v)
        if isinstance(v, dict):
            return {k: _normalize(v[k]) for k in sorted(v)}
        return v

    payload = json.dumps([_normalize(p) for p in parts], default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── Lookup helpers ────────────────────────────────────────────────────


async def _lookup_exact(
    *,
    session_factory: async_sessionmaker,
    conversation_id: _uuid.UUID,
    turn_id: int,
    agent_kind: AgentKind,
    target_id: str,
    shard_index: int,
) -> AgentResult | None:
    """Stage 1: same-turn exact match. The resume path."""
    async with session_factory() as session:
        row = (
            await session.execute(
                select(AgentResult)
                .where(AgentResult.conversation_id == conversation_id)
                .where(AgentResult.turn_id == turn_id)
                .where(AgentResult.agent_kind == agent_kind)
                .where(AgentResult.target_id == target_id)
                .where(AgentResult.shard_index == shard_index)
                .where(AgentResult.status == "success"),
            )
        ).scalar_one_or_none()
        return row


async def _lookup_semantic(
    *,
    session_factory: async_sessionmaker,
    agent_kind: AgentKind,
    target_id: str,
    shard_index: int,
    embedding: list[float],
    repo_commit_sha: str | None,
    ttl_seconds: int,
    similarity_threshold: float,
) -> tuple[AgentResult, float] | None:
    """Stage 2: pgvector ANN cosine match scoped to the same kind +
    target + shard, within freshness, and (for code) bound to the
    same commit sha. Returns ``(row, similarity)`` for the best
    match if its similarity is ≥ ``similarity_threshold``, else
    None.

    pgvector ``<=>`` is cosine distance (0 = identical, 2 = opposite).
    Similarity = ``1 - distance``. We pull the top-1 match scoped by
    the exact prefix and check its similarity against the threshold.
    """
    if not embedding:
        return None

    # pgvector's `<=>` operator returns cosine distance. We want
    # similarity = 1 - distance. The HNSW index returns nearest
    # neighbours sorted by distance ASC.
    distance_bound = 1.0 - similarity_threshold

    # Explicit casts on every bound parameter — asyncpg can't infer
    # the type of a NULL parameter from context, so without these
    # the query fails with "could not determine data type of
    # parameter $N" for ``repo_commit_sha=None`` (the common case).
    #
    # TTL filter removed (was: ``created_at > NOW() -
    # ttl_seconds``). Bifrost is a knowledge BASE, not a TTL cache;
    # rows live forever. Stale data is surfaced with
    # ``last_observed_at`` metadata so callers / agents can decide
    # whether to re-verify, instead of being silently dropped.
    # Commit-sha binding stays for code agents — that's correctness,
    # not freshness (a different commit is genuinely a different
    # codebase).
    sql = text(
        """
        SELECT
            ar.id,
            ar.research_run_id,
            ar.conversation_id,
            ar.turn_id,
            ar.agent_kind,
            ar.target_id,
            ar.shard_index,
            ar.scope_hash,
            ar.status,
            ar.output_json,
            ar.cost_usd,
            ar.duration_ms,
            ar.question_embedding,
            ar.repo_commit_sha,
            ar.ttl_seconds,
            ar.was_cache_hit,
            ar.source_run_id,
            ar.created_at,
            (ar.question_embedding <=> CAST(:embedding AS vector)) AS distance
        FROM agent_results ar
        WHERE ar.status = 'success'
          AND ar.question_embedding IS NOT NULL
          AND ar.was_cache_hit = false
          AND ar.agent_kind = CAST(:agent_kind AS text)
          AND ar.target_id = CAST(:target_id AS text)
          AND ar.shard_index = CAST(:shard_index AS integer)
          AND (
                CAST(:repo_commit_sha AS text) IS NULL
                OR ar.repo_commit_sha = CAST(:repo_commit_sha AS text)
              )
        ORDER BY ar.question_embedding <=> CAST(:embedding AS vector)
        LIMIT 1
        """,
    )

    async with session_factory() as session:
        result = await session.execute(
            sql,
            {
                "embedding": str(list(embedding)),
                "agent_kind": agent_kind,
                "target_id": target_id,
                "shard_index": shard_index,
                "repo_commit_sha": repo_commit_sha,
            },
        )
        row = result.mappings().first()
        if row is None:
            return None
        distance = float(row["distance"])
        if distance > distance_bound:
            return None

        # Materialise as an AgentResult so callers don't deal with
        # raw row dicts. The ORM session went out of scope; rebuild.
        ar = AgentResult(
            id=row["id"],
            research_run_id=row["research_run_id"],
            conversation_id=row["conversation_id"],
            turn_id=row["turn_id"],
            agent_kind=row["agent_kind"],
            target_id=row["target_id"],
            shard_index=row["shard_index"],
            scope_hash=row["scope_hash"],
            status=row["status"],
            output_json=row["output_json"],
            cost_usd=float(row["cost_usd"] or 0),
            duration_ms=int(row["duration_ms"] or 0),
            repo_commit_sha=row["repo_commit_sha"],
            ttl_seconds=int(row["ttl_seconds"] or 0),
            was_cache_hit=bool(row["was_cache_hit"]),
            source_run_id=row["source_run_id"],
            created_at=row["created_at"],
        )
        similarity = 1.0 - distance
        return ar, similarity


# ── Persist + bookkeeping ─────────────────────────────────────────────


async def _persist(
    *,
    session_factory: async_sessionmaker,
    research_run_id: _uuid.UUID,
    conversation_id: _uuid.UUID,
    turn_id: int,
    agent_kind: AgentKind,
    target_id: str,
    shard_index: int,
    scope_hash: str,
    status: str,
    output_json: dict[str, Any],
    cost_usd: float,
    duration_ms: int,
    question_embedding: list[float] | None,
    repo_commit_sha: str | None,
    ttl_seconds: int,
    was_cache_hit: bool = False,
    source_run_id: _uuid.UUID | None = None,
) -> None:
    """Insert one row. Conflicts on the per-turn unique key fall
    through silently — a parallel write won the race, the row is
    there, no data loss."""
    async with session_factory() as session:
        stmt = (
            pg_insert(AgentResult)
            .values(
                research_run_id=research_run_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                agent_kind=agent_kind,
                target_id=target_id,
                shard_index=shard_index,
                scope_hash=scope_hash,
                status=status,
                output_json=output_json,
                cost_usd=cost_usd,
                duration_ms=duration_ms,
                question_embedding=question_embedding,
                repo_commit_sha=repo_commit_sha,
                ttl_seconds=ttl_seconds,
                was_cache_hit=was_cache_hit,
                source_run_id=source_run_id,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    "conversation_id",
                    "turn_id",
                    "agent_kind",
                    "target_id",
                    "shard_index",
                ],
            )
        )
        logger.debug(
            "agent_cache._persist kind=%s target=%s run=%s turn=%d status=%s cost=$%.4f hit=%s",
            agent_kind,
            target_id,
            str(research_run_id)[:8],
            turn_id,
            status,
            cost_usd,
            was_cache_hit,
        )
        await session.execute(stmt)
        await session.commit()


async def _credit_saved_cost(
    *,
    session_factory: async_sessionmaker,
    research_run_id: _uuid.UUID,
    saved_usd: float,
) -> None:
    """Atomically increment ``research_runs.saved_cost_usd``. We use
    a SQL-level addition (``saved_cost_usd + :delta``) so concurrent
    hits for the same run don't lose updates."""
    if saved_usd <= 0:
        return
    async with session_factory() as session:
        await session.execute(
            update(ResearchRun)
            .where(ResearchRun.id == research_run_id)
            .values(saved_cost_usd=ResearchRun.saved_cost_usd + saved_usd),
        )
        await session.commit()


# ── Public entry point ────────────────────────────────────────────────


async def cached_agent[T](
    *,
    session_factory: async_sessionmaker,
    research_run_id: _uuid.UUID | None,
    conversation_id: _uuid.UUID,
    turn_id: int,
    agent_kind: AgentKind,
    target_id: str,
    shard_index: int,
    scope_hash: str,
    runner: Callable[[], Awaitable[T]],
    serialize: Callable[[T], dict[str, Any]],
    deserialize: Callable[[dict[str, Any]], T],
    extract_cost: Callable[[T], float] | None = None,
    question_embedding: list[float] | None = None,
    repo_commit_sha: str | None = None,
    similarity_threshold: float | None = None,
    on_cache_hit: Callable[[float], None] | None = None,
    is_cacheable: Callable[[T], bool] | None = None,
) -> tuple[T, Literal["exact", "semantic", "miss"]]:
    """Two-stage cache: exact-turn → semantic-cross-run → miss.

    Returns ``(report, hit_kind)`` where ``hit_kind`` is one of:
      - ``"exact"``  — Stage 1 hit (this turn was previously run)
      - ``"semantic"`` — Stage 2 hit (cross-run paraphrase match)
      - ``"miss"``   — fresh run

    Cost accounting:
      - On Stage 2 hit, ``research_runs.saved_cost_usd`` is credited
        with the source row's ``cost_usd`` and ``on_cache_hit`` is
        called with that amount.
      - On Stage 3 miss, ``extract_cost(report)`` (if provided) is
        called to capture the real spend, persisted on the row.

    ``research_run_id=None`` disables caching entirely (legacy
    callers / unit tests).

    ``question_embedding=None`` skips Stage 2 (no semantic key) but
    still does Stage 1 + Stage 3.

    ``is_cacheable`` (optional): a predicate over the fresh report. When it
    returns ``False`` the result is RETURNED to the caller but NOT persisted —
    so a FAILED agent run (e.g. a code agent that errored under contention)
    never poisons the cache and a re-run actually re-invokes the runner (which
    can retry and succeed). Without this guard a single transient failure would
    be replayed for every paraphrase of the question forever. Defaults to
    "cache everything" for backward compatibility.
    """
    if research_run_id is None:
        return await runner(), "miss"

    threshold = similarity_threshold if similarity_threshold is not None else _settings_threshold()
    ttl = _KIND_TTL.get(agent_kind, 3600)

    # ── Stage 1: exact-turn ───────────────────────────────────────────
    try:
        cached = await _lookup_exact(
            session_factory=session_factory,
            conversation_id=conversation_id,
            turn_id=turn_id,
            agent_kind=agent_kind,
            target_id=target_id,
            shard_index=shard_index,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "agent_cache exact-lookup failed (kind=%s target=%s); falling through to fresh run",
            agent_kind,
            target_id,
        )
        cached = None

    if cached is not None:
        try:
            report = deserialize(cached.output_json)
        except Exception:  # noqa: BLE001
            logger.exception(
                "agent_cache exact deserialize failed (kind=%s); running fresh",
                agent_kind,
            )
        else:
            return report, "exact"

    # ── Stage 2: cross-run semantic ───────────────────────────────────
    semantic_hit: tuple[AgentResult, float] | None = None
    if question_embedding is not None:
        try:
            semantic_hit = await _lookup_semantic(
                session_factory=session_factory,
                agent_kind=agent_kind,
                target_id=target_id,
                shard_index=shard_index,
                embedding=list(question_embedding),
                repo_commit_sha=repo_commit_sha,
                ttl_seconds=ttl,
                similarity_threshold=threshold,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "agent_cache semantic lookup failed (kind=%s); running fresh",
                agent_kind,
            )
            semantic_hit = None

    if semantic_hit is not None:
        source_row, similarity = semantic_hit
        try:
            report = deserialize(source_row.output_json)
        except Exception:  # noqa: BLE001
            logger.exception(
                "agent_cache semantic deserialize failed (kind=%s)",
                agent_kind,
            )
        else:
            saved_cost = float(source_row.cost_usd or 0.0)
            logger.info(
                "bifrost.cache.hit kind=%s target=%s shard=%d sim=%.3f saved=$%.4f source_run=%s",
                agent_kind,
                target_id,
                shard_index,
                similarity,
                saved_cost,
                source_row.research_run_id,
            )
            # Persist a thin attribution row for THIS turn so a future
            # exact-resume of this turn sees the cached output.
            try:
                await _persist(
                    session_factory=session_factory,
                    research_run_id=research_run_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    agent_kind=agent_kind,
                    target_id=target_id,
                    shard_index=shard_index,
                    scope_hash=scope_hash,
                    status="success",
                    output_json=source_row.output_json,
                    cost_usd=0.0,  # we paid 0 for this hit
                    duration_ms=0,
                    question_embedding=list(question_embedding) if question_embedding else None,
                    repo_commit_sha=repo_commit_sha,
                    ttl_seconds=ttl,
                    was_cache_hit=True,
                    source_run_id=source_row.research_run_id,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "agent_cache hit-attribution persist failed; "
                    "non-fatal (cache hit still returned)",
                )
            try:
                await _credit_saved_cost(
                    session_factory=session_factory,
                    research_run_id=research_run_id,
                    saved_usd=saved_cost,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "agent_cache saved-cost credit failed; non-fatal",
                )
            if on_cache_hit is not None:
                try:
                    on_cache_hit(saved_cost)
                except Exception:  # noqa: BLE001
                    logger.exception("on_cache_hit callback failed")
            return report, "semantic"

    # ── Stage 3: miss → run fresh ─────────────────────────────────────
    started = time.monotonic()
    report = await runner()
    duration_ms = int((time.monotonic() - started) * 1000)
    cost_usd = 0.0
    if extract_cost is not None:
        try:
            cost_usd = float(extract_cost(report) or 0.0)
        except Exception:  # noqa: BLE001
            logger.exception(
                "agent_cache extract_cost failed (kind=%s); row will record cost=0",
                agent_kind,
            )

    # Cache-poisoning guard: if the fresh report is a failure (per the
    # caller's predicate), do NOT persist it. Leaving a clean miss means
    # the next run re-executes the runner (which can retry and succeed),
    # rather than the semantic stage replaying a stale failure forever.
    if is_cacheable is not None and not is_cacheable(report):
        logger.warning(
            "agent_cache: NOT caching failed %s report (target=%s) — "
            "leaving a miss so the next run re-executes",
            agent_kind,
            target_id,
        )
        return report, "miss"

    try:
        await _persist(
            session_factory=session_factory,
            research_run_id=research_run_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            agent_kind=agent_kind,
            target_id=target_id,
            shard_index=shard_index,
            scope_hash=scope_hash,
            status="success",
            output_json=serialize(report),
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            question_embedding=list(question_embedding) if question_embedding else None,
            repo_commit_sha=repo_commit_sha,
            ttl_seconds=ttl,
            was_cache_hit=False,
            source_run_id=None,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "agent_cache persist (miss) failed (kind=%s target=%s); non-fatal",
            agent_kind,
            target_id,
        )

    return report, "miss"


async def count_cache_hits_for_run(
    session_factory: async_sessionmaker,
    research_run_id: _uuid.UUID,
) -> int:
    """How many semantic-hit attribution rows exist for this run.
    Used by the coordinator's run-finalisation event to populate
    ``phase_data.cache_hits``."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(AgentResult.id)
                .where(
                    AgentResult.research_run_id == research_run_id,
                )
                .where(AgentResult.was_cache_hit.is_(True)),
            )
        ).all()
        return len(rows)


# ── Cost extractors per Report dataclass ───────────────────────────────
#
# Each per-source agent's report carries an optional ``usage: Usage |
# None`` field whose ``total_cost_usd`` reflects the actual API spend
# of that agent's claude -p invocation. Pull it cleanly so the cache
# row records honest cost.


def cost_from_usage(report: Any) -> float:
    """Generic extractor for any Report dataclass with ``usage`` field.

    Returns 0.0 if usage is absent or malformed — caller's choice
    whether that's recorded as a 0 row (cheap to recompute) or
    skipped from the cache.
    """
    if report is None:
        return 0.0
    usage = getattr(report, "usage", None)
    if usage is None:
        return 0.0
    cost = getattr(usage, "total_cost_usd", None)
    if cost is None and isinstance(usage, dict):
        cost = usage.get("total_cost_usd")
    try:
        return float(cost or 0.0)
    except (TypeError, ValueError):
        return 0.0
