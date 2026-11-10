"""Write path: chat-research turns produce confidence_deltas + new edges.

The chat-research learning loop's second half. After the coordinator
finishes a turn the resulting ``RepoBundle`` carries (a) the code
agent's edge_verifications + missed_edges (verified live against the
clone) and (b) per-source-agent reports (jira/confluence/github/slack)
that confirm or contradict edges already in ``entity_relationships``.

This module walks those signals and converts them into governed graph
mutations via two existing primitives:

  * ``learning.record_delta(...)`` for confidence updates on existing
    relationship rows. Reasons: ``audit_confirmed`` (verified=true) and
    ``audit_contradicted`` (verified=false). The chat-research origin
    pointer is the ``conversation_id`` + ``research_run_id`` pair added
    by the baseline migration. Governance — downgrade-immediate, upgrade-needs-≥2-
    distinct-conversations — is unchanged from ``actions/learning.py``.
  * Direct ``EntityRelationship`` insert (ON CONFLICT DO NOTHING) for
    *new* edges the code agent found that weren't in the index. The
    edge's ``source_basis`` is tagged with ``code_verified`` (T1) and
    ``method="chat_research_audit"`` so a future audit can trace the
    edge back to the conversation that introduced it.

The function is **idempotent** — replaying the same bundle yields no
new state. ``record_delta`` already de-duplicates via the
``confidence_deltas`` audit-log row uniqueness; the EntityRelationship
``UniqueConstraint(source_id, target_id, rel_type)`` covers new-edge
inserts.

Failures here are non-fatal: if the audit pass raises, the turn's user-
visible answer is unaffected. The caller (``query_worker``) catches
exceptions, logs them, and proceeds. The learning loop catching up
on a missed turn is preferable to a working turn breaking because of
a graph-write hiccup.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.actions.learning import record_delta
from bifrost.research.confidence_policy import build_source_basis
from bifrost.storage.models import Entity, EntityRelationship
from bifrost.storage.sql_util import affected_rows

logger = logging.getLogger(__name__)


# ── audit caps ────────────────────────────────────────────────────────────────
#
# Bound the per-turn write blast radius: even on a hot turn we don't
# want to fire 100 record_delta calls. The code agent caps itself at
# 1-3 edge_verifications per repo per turn (see
# ``_REPO_AGENT_SYSTEM_PROMPT``); these are just safety nets.
_MAX_VERIFICATIONS_PER_REPO = 8
_MAX_NEW_EDGES_PER_REPO = 8


# Deltas to apply when an edge is audited:
#   verified=true  → bump toward 0.95 (T1 floor)
#   verified=false → drop toward 0.30 (uncertain — single chat-turn
#                    contradiction shouldn't zero out a confirmed edge,
#                    but it should pull the confidence into the band
#                    where another contradiction will retract it)
_AUDIT_CONFIRMED_TARGET = 0.92
_AUDIT_CONTRADICTED_TARGET = 0.30


@dataclass
class AuditStats:
    """One turn's chat-audit outcome — emitted as an SSE event."""

    edges_confirmed: int = 0
    edges_contradicted: int = 0
    new_edges_inserted: int = 0
    deltas_recorded: int = 0
    edges_skipped: int = 0  # subject/target couldn't be resolved
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


# ── name → entity URN resolution ──────────────────────────────────────────────
#
# The code agent reports edges as ``"OrderRouter --calls--> FulfillmentExecutor"``
# (edge_verifications) and ``MissedEdge(source_name, rel_type, target_name)``
# (missed_edges). To map back to ``entities.id`` we look up by:
#   1. Exact qualified_name match (most precise)
#   2. Exact name match within the bundle's repo scope (for components)
#   3. Fall back to full-table name match (cross-repo edges)
#
# Components have URN form ``urn:bifrost:repo/{repo_id}/component/{name}``
# but ``name`` is also stored on the row, so we don't need to construct
# the URN ourselves.


def _parse_edge_desc(edge_desc: str) -> tuple[str, str, str] | None:
    """Parse 'Source --rel_type--> Target' into (src, rel, tgt).

    Returns ``None`` if the format isn't recognised. The agent prompt
    pins the format ``Source --rel_type--> Target`` but real LLM output
    drifts — be liberal: also accepts ``Source -> rel_type -> Target``,
    ``Source --rel_type-- Target``, etc. The strategy: split on the
    arrow ``-->`` if present, otherwise on standalone ``>``, then strip
    surrounding dashes/whitespace from the middle relation token.
    """
    if not edge_desc:
        return None
    s = edge_desc.strip()

    # Try the canonical "src --rel--> tgt" form first.
    if "-->" in s:
        # split on FIRST "-->" → left = "src --rel", right = "tgt"
        left, _, right = s.partition("-->")
        right = right.strip()
        # left is "src --rel"; split on last "--" or run of dashes
        left_stripped = left.rstrip("- ").lstrip()
        # Find the last whitespace-then-dash boundary inside left
        # e.g. "OrderRouter --calls" → split on "--" → ["OrderRouter", "calls"]
        if "--" in left_stripped:
            src_chunk, _, rel_chunk = left_stripped.rpartition("--")
            src = src_chunk.strip().rstrip("- ")
            rel = rel_chunk.strip().lstrip("- ")
            if src and rel and right:
                return src, rel, right

    # Fallback: split on " -> " arrows
    if " -> " in s:
        parts = [p.strip() for p in s.split(" -> ") if p.strip()]
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]

    return None


async def _resolve_name_to_entity_id(
    session: AsyncSession,
    *,
    name: str,
    repo_id: _uuid.UUID | None = None,
) -> _uuid.UUID | None:
    """Find an entity row whose ``name`` or ``qualified_name`` matches.

    Repo-scoped match preferred; falls back to repo-agnostic match. Returns
    None if no entity is found — the audit just skips that edge then.
    """
    if not name:
        return None

    # Repo-scoped first (more reliable)
    if repo_id is not None:
        row = (
            await session.execute(
                select(Entity.id)
                .where(Entity.scope_repo_id == repo_id)
                .where((Entity.name == name) | (Entity.qualified_name == name))
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            return row

    # Cross-repo fallback (cross-repo edges in audit point at services
    # in OTHER repos — e.g., SHIP -> PriceCatalog). We pick the
    # newest-seen match to keep behaviour deterministic when duplicates
    # exist.
    row = (
        await session.execute(
            select(Entity.id)
            .where((Entity.name == name) | (Entity.qualified_name == name))
            .order_by(Entity.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def _record_relationship_delta(
    session: AsyncSession,
    *,
    relationship_id: _uuid.UUID,
    confirmed: bool,
    conversation_id: str | None,
    research_run_id: str | None,
) -> bool:
    """Wrap ``record_delta`` with the audit-mode arguments.

    Returns True on a successful insert (regardless of whether the
    delta was applied vs queued as pending_upgrade), False on error.
    """
    target = _AUDIT_CONFIRMED_TARGET if confirmed else _AUDIT_CONTRADICTED_TARGET
    reason = "audit_confirmed" if confirmed else "audit_contradicted"
    try:
        await record_delta(
            session,
            target_kind="relationship",
            target_id=str(relationship_id),
            proposed_confidence=target,
            reason=reason,
            conversation_id=conversation_id,
            research_run_id=research_run_id,
            actor="chat_research",
        )
        return True
    except Exception as exc:  # pragma: no cover - non-fatal
        logger.warning(
            "chat_audit: record_delta failed for relationship %s: %s",
            relationship_id,
            exc,
        )
        return False


async def _walk_repo_audit(
    session: AsyncSession,
    *,
    repo_bundle_repo_id: _uuid.UUID,
    code_report,  # RepoReport
    conversation_id: str | None,
    research_run_id: str | None,
    stats: AuditStats,
) -> None:
    """Audit one repo's edge_verifications + missed_edges.

    See module docstring for the governance contract: confirmations bump
    toward 0.92, contradictions drop toward 0.30, new edges insert as
    T1 with ``method="chat_research_audit"`` provenance.
    """
    if code_report is None:
        return

    # ── 1. edge_verifications: confirm-or-contradict existing edges ──
    verifications = list(code_report.edge_verifications or [])[:_MAX_VERIFICATIONS_PER_REPO]
    for v in verifications:
        verdict = (v.verified or "").lower()
        if verdict not in ("true", "false"):
            # "unknown" or empty — agent didn't reach a decision; skip
            stats.edges_skipped += 1
            continue
        parsed = _parse_edge_desc(v.edge_desc)
        if parsed is None:
            stats.edges_skipped += 1
            continue
        src_name, rel_type, tgt_name = parsed

        src_id = await _resolve_name_to_entity_id(
            session,
            name=src_name,
            repo_id=repo_bundle_repo_id,
        )
        tgt_id = await _resolve_name_to_entity_id(
            session,
            name=tgt_name,
            repo_id=repo_bundle_repo_id,
        )
        if src_id is None or tgt_id is None:
            stats.edges_skipped += 1
            continue

        # Find the relationship row (if any). If the code agent verified
        # an edge that doesn't exist in the graph, we treat it as a
        # missed-edge: insert as new T1 evidence rather than silently
        # losing the signal.
        rel_id = (
            await session.execute(
                select(EntityRelationship.id)
                .where(EntityRelationship.source_id == src_id)
                .where(EntityRelationship.target_id == tgt_id)
                .where(EntityRelationship.rel_type == rel_type)
                .limit(1)
            )
        ).scalar_one_or_none()

        if rel_id is None:
            if verdict == "true":
                # Verified-true edge missing from graph → treat like missed_edge
                await _insert_new_edge(
                    session,
                    source_id=src_id,
                    target_id=tgt_id,
                    rel_type=rel_type,
                    evidence=v.evidence or "",
                    conversation_id=conversation_id,
                    research_run_id=research_run_id,
                    stats=stats,
                )
            else:
                # Contradicted edge that doesn't exist → no-op
                stats.edges_skipped += 1
            continue

        ok = await _record_relationship_delta(
            session,
            relationship_id=rel_id,
            confirmed=(verdict == "true"),
            conversation_id=conversation_id,
            research_run_id=research_run_id,
        )
        if ok:
            stats.deltas_recorded += 1
            if verdict == "true":
                stats.edges_confirmed += 1
            else:
                stats.edges_contradicted += 1

    # ── 2. missed_edges: new T1 edges the agent found live ───────────
    missed = list(code_report.missed_edges or [])[:_MAX_NEW_EDGES_PER_REPO]
    for m in missed:
        src_id = await _resolve_name_to_entity_id(
            session,
            name=m.source_name,
            repo_id=repo_bundle_repo_id,
        )
        tgt_id = await _resolve_name_to_entity_id(
            session,
            name=m.target_name,
            repo_id=repo_bundle_repo_id,
        )
        if src_id is None or tgt_id is None or src_id == tgt_id:
            stats.edges_skipped += 1
            continue
        await _insert_new_edge(
            session,
            source_id=src_id,
            target_id=tgt_id,
            rel_type=m.rel_type or "related_to",
            evidence=m.evidence or "",
            conversation_id=conversation_id,
            research_run_id=research_run_id,
            stats=stats,
        )


async def _insert_new_edge(
    session: AsyncSession,
    *,
    source_id: _uuid.UUID,
    target_id: _uuid.UUID,
    rel_type: str,
    evidence: str,
    conversation_id: str | None,
    research_run_id: str | None,
    stats: AuditStats,
) -> None:
    """Insert one edge with ON CONFLICT DO NOTHING.

    Tagged ``code_verified`` (T1, ``method="chat_research_audit"``) so the
    edge inherits the same trust band as a normal extraction-time T1
    edge. ``conversation_id`` + ``research_run_id`` ride in the
    ``source_basis[*].method`` qualifier as a back-pointer.
    """
    if source_id == target_id:
        stats.edges_skipped += 1
        return

    reference = (
        evidence
        if evidence
        else f"chat_research:conv={conversation_id or '?'}:run={research_run_id or '?'}"
    )

    stmt = pg_insert(EntityRelationship).values(
        {
            "id": _uuid.uuid4(),
            "source_id": source_id,
            "target_id": target_id,
            "rel_type": rel_type,
            "confidence": 0.95,
            "freshness_state": "fresh",
            "source_basis": build_source_basis(
                "code_verified",
                reference=reference,
                method="chat_research_audit",
                matched_on=(f"conv={conversation_id or '?'};run={research_run_id or '?'}"),
                excerpt=(evidence[:200] if evidence else None),
            ),
        }
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["source_id", "target_id", "rel_type"])
    result = await session.execute(stmt)
    inserted = affected_rows(result)
    if inserted:
        stats.new_edges_inserted += inserted
    else:
        # Edge already existed — count as a "soft confirm" since the
        # code agent independently re-discovered it. This is a cheap win:
        # a chat run that re-finds an edge the extractor already knew
        # bumps that edge's confidence too.
        rel_id = (
            await session.execute(
                select(EntityRelationship.id)
                .where(EntityRelationship.source_id == source_id)
                .where(EntityRelationship.target_id == target_id)
                .where(EntityRelationship.rel_type == rel_type)
                .limit(1)
            )
        ).scalar_one_or_none()
        if rel_id is not None:
            ok = await _record_relationship_delta(
                session,
                relationship_id=rel_id,
                confirmed=True,
                conversation_id=conversation_id,
                research_run_id=research_run_id,
            )
            if ok:
                stats.deltas_recorded += 1
                stats.edges_confirmed += 1


# ── public entry point ────────────────────────────────────────────────────────


async def chat_research_audit(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    bundles,  # list[RepoBundle]
    conversation_id: str | None,
    research_run_id: str | None,
) -> AuditStats:
    """Walk all repo bundles in a turn and feed the audit signals into the DAG.

    Called from ``query_worker.run_query_turn`` after the assistant turn
    is persisted. Errors are caught + logged — a failed audit must not
    surface as a user-visible error.

    Returns ``AuditStats`` so the caller (UI + logs) can show
    "+3 edges confirmed, -1 contradicted, 2 new edges this turn".
    """
    stats = AuditStats()
    if not bundles:
        return stats

    async with session_factory() as session:
        for bundle in bundles:
            # repo_id on a bundle is a UUID string; coerce defensively.
            try:
                repo_uuid = _uuid.UUID(str(bundle.repo_id))
            except (TypeError, ValueError):
                continue
            try:
                await _walk_repo_audit(
                    session,
                    repo_bundle_repo_id=repo_uuid,
                    code_report=bundle.code,
                    conversation_id=conversation_id,
                    research_run_id=research_run_id,
                    stats=stats,
                )
            except Exception as exc:  # pragma: no cover - non-fatal
                logger.exception(
                    "chat_audit: error walking repo %s",
                    getattr(bundle, "repo_name", bundle.repo_id),
                )
                stats.errors.append(f"{bundle.repo_name}: {exc}")
        await session.commit()

    logger.info(
        "chat_audit: conv=%s run=%s confirmed=%d contradicted=%d new=%d skipped=%d errors=%d",
        conversation_id,
        research_run_id,
        stats.edges_confirmed,
        stats.edges_contradicted,
        stats.new_edges_inserted,
        stats.edges_skipped,
        len(stats.errors),
    )
    return stats
