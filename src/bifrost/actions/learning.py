"""Continuous-learning loop — turn task outcomes into graph updates.

The loop in one sentence
------------------------
Every time an agent cites an entity or edge, or a task reaches a
terminal state, or a user confirms / contradicts something in the
admin UI, we write a row to ``confidence_deltas``. A governance
function then decides whether the underlying ``entities.confidence`` /
``entity_relationships.confidence`` should actually move — and by how
much.

Governance rules (encoded here; the single place to change them)
----------------------------------------------------------------
* **Downgrades apply immediately.** One contradicting signal is enough
  to pull confidence down. Low-confidence data rotting into the graph
  is worse than still-uncertain data being flagged.
* **Upgrades need 2 independent confirmations from distinct tasks.**
  Anyone can rubber-stamp their own task's output; cross-task
  agreement is what we trust. Until the second confirmation lands we
  write the delta with ``applied_by = 'pending_upgrade'`` and leave
  the target untouched. Once satisfied we promote the pending row(s).
* **Noise floor.** Deltas with |Δ| < 0.03 are recorded for audit but
  never drive a write to the target. Keeps the graph stable under a
  steady drip of tiny corrections. The floor (0.03) sits deliberately
  *below* the standard upgrade step (+0.08, see conversations feedback
  route) so a routine confirmation always clears it — a floor equal to
  the step size silently swallowed every near-cap upgrade.
* **Upgrade target = max(current, new_confidence).** So a pending
  cluster of confirmations can't downgrade on promotion. We take the
  strongest positive signal among them.

Everything else (who wrote the delta, which task triggered it, which
assumption or plan cited the target) lives in ``confidence_deltas`` for
audit — the governance logic here only looks at ``target_kind +
target_id + applied_by``.
"""

from __future__ import annotations

import json
import logging
import uuid as _uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ── Types + constants ────────────────────────────────────────────────


TargetKind = Literal["entity", "relationship"]
DeltaReason = Literal[
    "task_verify_pass",
    "task_verify_fail",
    "correction_retraction",
    "re_extraction",
    "audit_confirmed",
    "audit_contradicted",
]
# Must match the agent_judgements.outcome CHECK constraint (baseline migration).
# The earlier short spellings (confirmed/contradicted/stale) were never in
# the DB CHECK, so any resolve_judgement() call with them would have raised
# a CheckViolationError. Aligned to the DB vocabulary.
JudgementOutcome = Literal[
    "pending",
    "confirmed_by_user",
    "corrected_by_user",
    "confirmed_by_verify",
    "contradicted_by_verify",
]


UPGRADE_CONFIRMATIONS_NEEDED = 2
# Floor below which a delta is audit-only (never writes the target).
# MUST stay below the upgrade step size (currently +0.08 in the chat
# feedback route) — when floor == step, a +step upgrade clears the floor
# by exactly zero and any capping near confidence=1.0 pushes it under,
# so high-confidence entities could never be upgraded. 0.03 leaves
# comfortable headroom.
NOISE_FLOOR = Decimal("0.03")

# Levels we'll write into new_level when updating a target. Sourced from
# the entities.confidence_level CHECK constraint.
_LEVEL_CONFIRMED = "confirmed"
_LEVEL_INFERRED = "inferred"
_LEVEL_UNCERTAIN = "uncertain"
_LEVEL_CONFLICTING = "conflicting"


def _level_from_confidence(c: Decimal) -> str:
    if c >= Decimal("0.85"):
        return _LEVEL_CONFIRMED
    if c >= Decimal("0.60"):
        return _LEVEL_INFERRED
    return _LEVEL_UNCERTAIN


def _tier_from_level(level: str) -> int:
    return {
        "confirmed": 1,
        "inferred": 3,
        "stale": 4,
        "uncertain": 5,
        "unknown": 5,
        "conflicting": 4,
    }.get(level, 5)


@dataclass
class DeltaApplication:
    """The result of attempting to record+apply a confidence delta."""

    delta_id: str
    target_kind: TargetKind
    target_id: str
    old_confidence: float | None
    proposed_new_confidence: float
    applied_new_confidence: float | None  # None = not applied (pending/noise)
    direction: Literal["up", "down", "noop"]
    applied_by: str  # auto_downgrade | auto_upgrade | pending_upgrade | noise | user | agent:*
    promoted_delta_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ── Target readers ──────────────────────────────────────────────────


async def _read_target(
    session: AsyncSession,
    target_kind: TargetKind,
    target_id: str,
) -> tuple[Decimal, str, int] | None:
    """Return (confidence, level, tier) for the target, or None if absent.

    Relationships don't carry level/tier columns, so we derive them
    from the current confidence value.

    The SELECT takes a ``FOR UPDATE`` row lock so concurrent deltas on
    the same target serialize. Without it, two simultaneous
    confirmations could both read the same pending set, both clear the
    confirmation gate, and both promote/write the target (double-apply).
    The lock is held until the request's transaction commits — the whole
    record_delta read→decide→write sequence runs under it.
    """
    if target_kind == "entity":
        row = (
            await session.execute(
                text("SELECT confidence, confidence_level FROM entities WHERE id = :id FOR UPDATE"),
                {"id": target_id},
            )
        ).first()
        if row is None:
            return None
        conf = Decimal(str(row[0]))
        level = row[1] or _level_from_confidence(conf)
        return conf, level, _tier_from_level(level)

    if target_kind == "relationship":
        row = (
            await session.execute(
                text("SELECT confidence FROM entity_relationships WHERE id = :id FOR UPDATE"),
                {"id": target_id},
            )
        ).first()
        if row is None:
            return None
        conf = Decimal(str(row[0]))
        level = _level_from_confidence(conf)
        return conf, level, _tier_from_level(level)

    return None


async def _write_target(
    session: AsyncSession,
    target_kind: TargetKind,
    target_id: str,
    new_confidence: Decimal,
    new_level: str,
) -> None:
    """Apply a confidence update to the target row."""
    if target_kind == "entity":
        await session.execute(
            text(
                """
                UPDATE entities
                SET confidence = :c,
                    confidence_level = :lvl,
                    updated_at = NOW()
                WHERE id = :id
                """
            ),
            {"c": new_confidence, "lvl": new_level, "id": target_id},
        )
    elif target_kind == "relationship":
        await session.execute(
            text(
                """
                UPDATE entity_relationships
                SET confidence = :c, updated_at = NOW()
                WHERE id = :id
                """
            ),
            {"c": new_confidence, "id": target_id},
        )


# ── Delta insert ────────────────────────────────────────────────────


async def _insert_delta(
    session: AsyncSession,
    *,
    target_kind: TargetKind,
    target_id: str,
    old_confidence: Decimal | None,
    new_confidence: Decimal,
    old_level: str | None,
    new_level: str,
    old_tier: int | None,
    new_tier: int,
    reason: DeltaReason,
    task_id: str | None,
    assumption_id: str | None,
    plan_id: str | None,
    conversation_id: str | None,
    research_run_id: str | None,
    applied_by: str,
) -> str:
    delta_id = str(_uuid.uuid4())
    await session.execute(
        text(
            """
            INSERT INTO confidence_deltas
                (id, target_kind, target_id, old_confidence, new_confidence,
                 old_level, new_level, old_tier, new_tier, reason,
                 task_id, assumption_id, plan_id,
                 conversation_id, research_run_id,
                 applied_at, applied_by)
            VALUES
                (:id, :tk, :tid, :oc, :nc, :ol, :nl, :ot, :nt, :r,
                 :bid, :aid, :pid, :cid, :rid, :at, :ab)
            """
        ),
        {
            "id": delta_id,
            "tk": target_kind,
            "tid": target_id,
            "oc": old_confidence,
            "nc": new_confidence,
            "ol": old_level,
            "nl": new_level,
            "ot": old_tier,
            "nt": new_tier,
            "r": reason,
            "bid": task_id,
            "aid": assumption_id,
            "pid": plan_id,
            "cid": conversation_id,
            "rid": research_run_id,
            "at": datetime.now(UTC),
            "ab": applied_by,
        },
    )
    return delta_id


# ── The main entry point ────────────────────────────────────────────


async def record_delta(
    session: AsyncSession,
    *,
    target_kind: TargetKind,
    target_id: str,
    proposed_confidence: Decimal | float,
    reason: DeltaReason,
    task_id: str | None = None,
    assumption_id: str | None = None,
    plan_id: str | None = None,
    conversation_id: str | None = None,
    research_run_id: str | None = None,
    actor: str = "system",
) -> DeltaApplication:
    """Record a confidence delta and apply governance.

    ``proposed_confidence`` is what the caller *wants* the confidence to
    become. The function decides whether that actually happens based on
    the governance rules at the top of this module.

    Origin pointers (≥1 should be set so the audit chain isn't orphaned):
    - ``task_id`` for brief-driven deltas (verify pass/fail, correction)
    - ``conversation_id`` + optional ``research_run_id`` for chat-driven
      deltas (audit_confirmed / audit_contradicted from a research turn)

    The upgrade rule needs ≥``UPGRADE_CONFIRMATIONS_NEEDED`` distinct
    *origins* to promote a pending upgrade. For brief-driven deltas,
    distinct ``task_id``s; for chat-driven deltas, distinct
    ``conversation_id``s. This keeps a chatty user from compounding
    one-source confirmations into a graph upgrade — same governance
    guarantee the action layer relies on.
    """
    proposed = Decimal(str(proposed_confidence)).quantize(Decimal("0.01"))
    proposed = max(Decimal("0.00"), min(Decimal("1.00"), proposed))

    cur = await _read_target(session, target_kind, target_id)
    if cur is None:
        raise ValueError(f"{target_kind} {target_id} not found")
    old_conf, old_level, old_tier = cur
    diff = proposed - old_conf
    new_level = _level_from_confidence(proposed)
    new_tier = _tier_from_level(new_level)

    # ─── Noise floor ─────────────────────────────────────────────
    if abs(diff) < NOISE_FLOOR:
        delta_id = await _insert_delta(
            session,
            target_kind=target_kind,
            target_id=target_id,
            old_confidence=old_conf,
            new_confidence=proposed,
            old_level=old_level,
            new_level=new_level,
            old_tier=old_tier,
            new_tier=new_tier,
            reason=reason,
            task_id=task_id,
            assumption_id=assumption_id,
            plan_id=plan_id,
            conversation_id=conversation_id,
            research_run_id=research_run_id,
            applied_by=f"noise:{actor}",
        )
        return DeltaApplication(
            delta_id=delta_id,
            target_kind=target_kind,
            target_id=target_id,
            old_confidence=float(old_conf),
            proposed_new_confidence=float(proposed),
            applied_new_confidence=None,
            direction="noop",
            applied_by=f"noise:{actor}",
        )

    # ─── Downgrade: apply immediately ───────────────────────────
    if diff < 0:
        delta_id = await _insert_delta(
            session,
            target_kind=target_kind,
            target_id=target_id,
            old_confidence=old_conf,
            new_confidence=proposed,
            old_level=old_level,
            new_level=new_level,
            old_tier=old_tier,
            new_tier=new_tier,
            reason=reason,
            task_id=task_id,
            assumption_id=assumption_id,
            plan_id=plan_id,
            conversation_id=conversation_id,
            research_run_id=research_run_id,
            applied_by=f"auto_downgrade:{actor}",
        )
        await _write_target(session, target_kind, target_id, proposed, new_level)
        return DeltaApplication(
            delta_id=delta_id,
            target_kind=target_kind,
            target_id=target_id,
            old_confidence=float(old_conf),
            proposed_new_confidence=float(proposed),
            applied_new_confidence=float(proposed),
            direction="down",
            applied_by=f"auto_downgrade:{actor}",
        )

    # ─── Upgrade: needs N independent confirmations ──────────────
    # Prior pending upgrades for this target. We pull both task_id
    # AND conversation_id so the diversity check can use whichever
    # origin fits the source of the delta — task-driven (briefs) or
    # conversation-driven (chat-research). A delta originating from
    # one origin doesn't compound with another origin's confirmation;
    # they're parallel confirmation paths.
    prior_pending = (
        await session.execute(
            text(
                """
                SELECT id, new_confidence, new_level, new_tier,
                       task_id, conversation_id
                FROM confidence_deltas
                WHERE target_kind = :tk AND target_id = :tid
                  AND applied_by LIKE 'pending_upgrade%'
                ORDER BY applied_at ASC
                """
            ),
            {"tk": target_kind, "tid": target_id},
        )
    ).all()

    distinct_tasks = {str(r[4]) for r in prior_pending if r[4] is not None}
    if task_id is not None:
        distinct_tasks.add(task_id)
    distinct_convs = {str(r[5]) for r in prior_pending if r[5] is not None}
    if conversation_id is not None:
        distinct_convs.add(conversation_id)

    # Either origin path can promote independently — task-driven
    # confirmations or conversation-driven confirmations. NOT a sum
    # across both: a single brief confirming twice doesn't count, nor
    # does a single conversation confirming twice. Two distinct
    # tasks OR two distinct conversations is the bar.
    ready_to_promote = (
        len(distinct_tasks) >= UPGRADE_CONFIRMATIONS_NEEDED
        or len(distinct_convs) >= UPGRADE_CONFIRMATIONS_NEEDED
    )

    if not ready_to_promote:
        delta_id = await _insert_delta(
            session,
            target_kind=target_kind,
            target_id=target_id,
            old_confidence=old_conf,
            new_confidence=proposed,
            old_level=old_level,
            new_level=new_level,
            old_tier=old_tier,
            new_tier=new_tier,
            reason=reason,
            task_id=task_id,
            assumption_id=assumption_id,
            plan_id=plan_id,
            conversation_id=conversation_id,
            research_run_id=research_run_id,
            applied_by=f"pending_upgrade:{actor}",
        )
        return DeltaApplication(
            delta_id=delta_id,
            target_kind=target_kind,
            target_id=target_id,
            old_confidence=float(old_conf),
            proposed_new_confidence=float(proposed),
            applied_new_confidence=None,
            direction="up",
            applied_by=f"pending_upgrade:{actor}",
        )

    # Promote: take max of (current + all pending + this proposed) as the
    # new confidence. Prior pending rows get their applied_by renamed so
    # they don't show up in future pending queries.
    max_proposed = max([proposed, *[Decimal(str(r[1])) for r in prior_pending]])
    promoted_level = _level_from_confidence(max_proposed)
    promoted_tier = _tier_from_level(promoted_level)

    delta_id = await _insert_delta(
        session,
        target_kind=target_kind,
        target_id=target_id,
        old_confidence=old_conf,
        new_confidence=max_proposed,
        old_level=old_level,
        new_level=promoted_level,
        old_tier=old_tier,
        new_tier=promoted_tier,
        reason=reason,
        task_id=task_id,
        assumption_id=assumption_id,
        plan_id=plan_id,
        conversation_id=conversation_id,
        research_run_id=research_run_id,
        applied_by=f"auto_upgrade:{actor}",
    )
    # Flip prior pending rows to "promoted" (audit trail intact).
    promoted_ids = [str(r[0]) for r in prior_pending]
    if promoted_ids:
        await session.execute(
            text(
                """
                UPDATE confidence_deltas
                SET applied_by = REPLACE(applied_by, 'pending_upgrade', 'promoted')
                WHERE id = ANY(:ids)
                """
            ),
            {"ids": promoted_ids},
        )
    await _write_target(
        session,
        target_kind,
        target_id,
        max_proposed,
        promoted_level,
    )
    return DeltaApplication(
        delta_id=delta_id,
        target_kind=target_kind,
        target_id=target_id,
        old_confidence=float(old_conf),
        proposed_new_confidence=float(proposed),
        applied_new_confidence=float(max_proposed),
        direction="up",
        applied_by=f"auto_upgrade:{actor}",
        promoted_delta_ids=promoted_ids,
    )


# ── Agent judgements ────────────────────────────────────────────────


async def record_judgement(
    session: AsyncSession,
    *,
    task_id: str,
    agent_kind: str,
    claim: str,
    phase_id: str | None = None,
    cited_urns: list[str] | None = None,
    cited_tiers: list[int] | None = None,
    run_id: str | None = None,
) -> str:
    """Capture an agent's claim so we can grade it later."""
    jid = str(_uuid.uuid4())
    await session.execute(
        text(
            """
            INSERT INTO agent_judgements
                (id, task_id, phase_id, agent_kind, run_id, claim,
                 cited_tiers, cited_urns, outcome)
            VALUES
                (:id, :b, :p, :k, :r, :c,
                 CAST(:tiers AS JSONB), CAST(:urns AS JSONB), 'pending')
            """
        ),
        {
            "id": jid,
            "b": task_id,
            "p": phase_id,
            "k": agent_kind,
            "r": run_id,
            "c": claim[:4000],
            "tiers": json.dumps(cited_tiers or []),
            "urns": json.dumps(cited_urns or []),
        },
    )
    return jid


async def resolve_judgement(
    session: AsyncSession,
    *,
    judgement_id: str,
    outcome: JudgementOutcome,
    correction_text: str | None = None,
) -> None:
    """Set the agent judgement's verdict after the task terminates."""
    await session.execute(
        text(
            """
            UPDATE agent_judgements
            SET outcome = :o,
                outcome_at = :at,
                correction_text = :ct
            WHERE id = :id
            """
        ),
        {
            "o": outcome,
            "at": datetime.now(UTC),
            "ct": correction_text,
            "id": judgement_id,
        },
    )


# ── Read helpers ────────────────────────────────────────────────────


async def list_deltas(
    session: AsyncSession,
    *,
    task_id: str | None = None,
    target_kind: TargetKind | None = None,
    target_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    where = ["TRUE"]
    params: dict[str, Any] = {"lim": limit}
    if task_id:
        where.append("task_id = :b")
        params["b"] = task_id
    if target_kind:
        where.append("target_kind = :tk")
        params["tk"] = target_kind
    if target_id:
        where.append("target_id = :tid")
        params["tid"] = target_id
    sql = (
        "SELECT id, target_kind, target_id, old_confidence, new_confidence, "
        "old_level, new_level, old_tier, new_tier, reason, task_id, "
        "assumption_id, plan_id, applied_at, applied_by "
        "FROM confidence_deltas "
        "WHERE " + " AND ".join(where) + " ORDER BY applied_at DESC LIMIT :lim"
    )
    rows = (await session.execute(text(sql), params)).all()
    return [
        {
            "id": str(r[0]),
            "target_kind": r[1],
            "target_id": str(r[2]),
            "old_confidence": float(r[3]) if r[3] is not None else None,
            "new_confidence": float(r[4]),
            "old_level": r[5],
            "new_level": r[6],
            "old_tier": int(r[7]) if r[7] is not None else None,
            "new_tier": int(r[8]),
            "reason": r[9],
            "task_id": str(r[10]) if r[10] else None,
            "assumption_id": str(r[11]) if r[11] else None,
            "plan_id": str(r[12]) if r[12] else None,
            "applied_at": r[13].isoformat(),
            "applied_by": r[14],
        }
        for r in rows
    ]


async def list_pending_upgrades(
    session: AsyncSession,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Targets with pending upgrades + how many distinct confirmations so far.

    Both origin paths count: distinct tasks (brief/task-driven deltas) and
    distinct conversations (chat-feedback deltas, where task_id is NULL).
    record_delta promotes on EITHER reaching the threshold, so this view
    must surface both — otherwise chat-origin pending upgrades that are
    actually ready show 0/N because their task_id is always NULL.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT
                    cd.target_kind,
                    cd.target_id::text,
                    COUNT(DISTINCT cd.task_id) AS distinct_tasks,
                    COUNT(DISTINCT cd.conversation_id) AS distinct_convs,
                    MAX(cd.new_confidence) AS max_proposed,
                    MIN(cd.applied_at) AS first_seen,
                    MAX(cd.applied_at) AS last_seen,
                    (
                        SELECT confidence FROM entities e WHERE e.id = cd.target_id
                        UNION ALL
                        SELECT confidence FROM entity_relationships r WHERE r.id = cd.target_id
                        LIMIT 1
                    ) AS current_confidence
                FROM confidence_deltas cd
                WHERE cd.applied_by LIKE 'pending_upgrade%'
                GROUP BY cd.target_kind, cd.target_id
                ORDER BY MIN(cd.applied_at) ASC
                LIMIT :lim
                """
            ),
            {"lim": limit},
        )
    ).all()
    return [
        {
            "target_kind": r[0],
            "target_id": r[1],
            "distinct_tasks": int(r[2]),
            "distinct_conversations": int(r[3]),
            # Effective confirmations = the stronger origin path, mirroring
            # record_delta's "tasks >= N OR conversations >= N" promotion rule.
            "confirmations": max(int(r[2]), int(r[3])),
            "confirmations_needed": UPGRADE_CONFIRMATIONS_NEEDED,
            "max_proposed_confidence": float(r[4]) if r[4] is not None else None,
            "current_confidence": float(r[7]) if r[7] is not None else None,
            "first_seen": r[5].isoformat() if r[5] else None,
            "last_seen": r[6].isoformat() if r[6] else None,
        }
        for r in rows
    ]


async def list_judgements(
    session: AsyncSession,
    *,
    task_id: str | None = None,
    outcome: JudgementOutcome | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    where = ["TRUE"]
    params: dict[str, Any] = {"lim": limit}
    if task_id:
        where.append("task_id = :b")
        params["b"] = task_id
    if outcome:
        where.append("outcome = :o")
        params["o"] = outcome
    sql = (
        "SELECT id, task_id, phase_id, agent_kind, run_id, claim, "
        "cited_tiers, cited_urns, outcome, outcome_at, correction_text, "
        "created_at FROM agent_judgements "
        "WHERE " + " AND ".join(where) + " ORDER BY created_at DESC LIMIT :lim"
    )
    rows = (await session.execute(text(sql), params)).all()
    return [
        {
            "id": str(r[0]),
            "task_id": str(r[1]),
            "phase_id": str(r[2]) if r[2] else None,
            "agent_kind": r[3],
            "run_id": r[4],
            "claim": r[5],
            "cited_tiers": r[6] or [],
            "cited_urns": r[7] or [],
            "outcome": r[8],
            "outcome_at": r[9].isoformat() if r[9] else None,
            "correction_text": r[10],
            "created_at": r[11].isoformat(),
        }
        for r in rows
    ]


async def learning_stats(session: AsyncSession) -> dict[str, Any]:
    """One-shot snapshot for the admin dashboard."""
    row = (
        await session.execute(
            text(
                """
                SELECT
                    COUNT(*)                                             AS total,
                    COUNT(*) FILTER (WHERE applied_by LIKE 'auto_downgrade%') AS downgrades,
                    COUNT(*) FILTER (WHERE applied_by LIKE 'auto_upgrade%')   AS upgrades,
                    COUNT(*) FILTER (WHERE applied_by LIKE 'pending_upgrade%') AS pending,
                    COUNT(*) FILTER (WHERE applied_by LIKE 'noise%')     AS noise,
                    -- Actionable = anything that actually moved a target or
                    -- is queued to (i.e. NOT audit-only noise). This is the
                    -- number that reflects real learning signal; `total`
                    -- is dominated by zero-magnitude citation breadcrumbs.
                    COUNT(*) FILTER (WHERE applied_by NOT LIKE 'noise%') AS actionable,
                    -- Flat = pure citation audit rows where proposed == current
                    -- (Δ=0). These exist only so the provenance chain isn't
                    -- orphaned; they can never drive a write. Surfacing the
                    -- count explains WHY `total` is so much larger than
                    -- `actionable`.
                    COUNT(*) FILTER (WHERE new_confidence = old_confidence) AS flat,
                    COUNT(*) FILTER (WHERE applied_at > NOW() - INTERVAL '7 days') AS last_7d,
                    COUNT(DISTINCT target_id)                            AS targets_touched,
                    COUNT(DISTINCT task_id)                             AS tasks_involved
                FROM confidence_deltas
                """
            )
        )
    ).first()
    jrow = (
        await session.execute(
            text(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE outcome = 'pending')      AS pending,
                    COUNT(*) FILTER (WHERE outcome = 'confirmed')    AS confirmed,
                    COUNT(*) FILTER (WHERE outcome = 'contradicted') AS contradicted
                FROM agent_judgements
                """
            )
        )
    ).first()
    return {
        "deltas": {
            "total": int(row[0] or 0),
            "auto_downgrades": int(row[1] or 0),
            "auto_upgrades": int(row[2] or 0),
            "pending_upgrades": int(row[3] or 0),
            "noise": int(row[4] or 0),
            "actionable": int(row[5] or 0),
            "flat": int(row[6] or 0),
            "last_7_days": int(row[7] or 0),
            "targets_touched": int(row[8] or 0),
            "tasks_involved": int(row[9] or 0),
        },
        "judgements": {
            "total": int(jrow[0] or 0),
            "pending": int(jrow[1] or 0),
            "confirmed": int(jrow[2] or 0),
            "contradicted": int(jrow[3] or 0),
        },
        "config": {
            "upgrade_confirmations_needed": UPGRADE_CONFIRMATIONS_NEEDED,
            "noise_floor": float(NOISE_FLOOR),
        },
    }
