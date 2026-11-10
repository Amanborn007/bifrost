"""Correction cascade — when an assumption changes, invalidate downstream.

A task's belief DAG looks like this:

    intent → assumptions (A1…An, via task_assumptions)
                    │
                    ▼  task_assumption_uses (M:M)
        task_artifacts (context/plan/…)
                    │
                    ▼  task_plans.derived_from_artifact_id
                task_plans (atomic tasks)

When the user answers a clarification and a later discussion round
retracts or changes an assumption, anything downstream that USED that
assumption is now operating on stale information.

This module's job is to walk the DAG after retraction and either:
  1. Mark dependent artifacts as ``stale`` (user will see a badge,
     click "Re-plan" to regenerate).
  2. Reject dependent ``task_plans`` rows that haven't executed yet
     (status: pending/approved → rejected).

Plans already executing or completed are *not* reverted automatically.
Those need a human decision — we surface them via the admin UI.

Signals
-------
Every cascaded change writes a confidence_deltas row with
``reason='correction_retraction'`` so the learning loop (Layer 4) can
spot which assumptions tend to produce churn and weight them lower in
future planning rounds.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.actions import learning as _learning
from bifrost.actions.task_lifecycle import record_event

logger = logging.getLogger(__name__)


@dataclass
class CascadeResult:
    retracted_assumption_ids: list[str]
    invalidated_artifact_ids: list[str] = field(default_factory=list)
    rejected_plan_ids: list[str] = field(default_factory=list)
    preserved_plan_ids: list[str] = field(default_factory=list)
    confidence_deltas_written: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Entry point ──────────────────────────────────────────────────────


async def cascade_retractions(
    session: AsyncSession,
    *,
    task_id: str,
    retracted_assumption_ids: list[str],
    actor: str = "agent:discuss",
) -> CascadeResult:
    """Walk the DAG downstream of each retracted assumption."""
    if not retracted_assumption_ids:
        return CascadeResult(retracted_assumption_ids=[])

    result = CascadeResult(retracted_assumption_ids=list(retracted_assumption_ids))

    # 1. Artifacts that used at least one retracted assumption
    affected_artifacts = (
        await session.execute(
            text(
                """
                SELECT DISTINCT u.artifact_id::text, a.kind, a.state, a.phase_id::text
                FROM task_assumption_uses u
                JOIN task_artifacts a ON a.id = u.artifact_id
                WHERE u.assumption_id = ANY(:ids)
                  AND a.state IN ('draft','approved')
                """
            ),
            {"ids": retracted_assumption_ids},
        )
    ).all()

    for art_id, kind, state, _phase_id in affected_artifacts:
        # Flip to 'stale' — the UI shows a badge; user clicks re-plan.
        await session.execute(
            text(
                """
                UPDATE task_artifacts SET state = 'stale'
                WHERE id = :a AND state IN ('draft','approved')
                """
            ),
            {"a": art_id},
        )
        result.invalidated_artifact_ids.append(art_id)
        logger.info(
            "cascade: marked %s artifact %s stale (was %s)",
            kind,
            art_id,
            state,
        )

    # 2. task_plans that cite a retracted assumption.
    #    JSONB @> requires a list; use ?| (any key overlap) via text cast.
    affected_plans = (
        await session.execute(
            text(
                """
                SELECT id::text, status, derived_from_artifact_id::text
                FROM task_plans
                WHERE EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(assumption_ids) aid
                    WHERE aid = ANY(:ids)
                )
                """
            ),
            {"ids": retracted_assumption_ids},
        )
    ).all()

    terminal_statuses = {"executing", "completed", "failed"}
    for plan_id, plan_status, _art in affected_plans:
        if plan_status in terminal_statuses:
            # Can't un-execute; preserve + let humans decide.
            result.preserved_plan_ids.append(plan_id)
            continue
        await session.execute(
            text(
                """
                UPDATE task_plans
                SET status = 'rejected'
                WHERE id = :id AND status IN ('pending','approved')
                """
            ),
            {"id": plan_id},
        )
        result.rejected_plan_ids.append(plan_id)

    # 3. Emit an event for audit
    await record_event(
        session,
        task_id=task_id,
        actor=actor,
        action="correction_cascade",
        payload={
            "retracted_assumptions": len(retracted_assumption_ids),
            "invalidated_artifacts": len(result.invalidated_artifact_ids),
            "rejected_plans": len(result.rejected_plan_ids),
            "preserved_plans": len(result.preserved_plan_ids),
        },
    )

    # 4. Write confidence_deltas for URNs cited by rejected plans.
    #    We don't downgrade the assumption itself (it's already retracted);
    #    we downgrade ENTITIES that the rejected plans pointed at, so the
    #    graph stops treating them as high-confidence.
    for plan_id in result.rejected_plan_ids:
        plan_row = (
            await session.execute(
                text(
                    """
                    SELECT urns FROM task_plans WHERE id = :id
                    """
                ),
                {"id": plan_id},
            )
        ).first()
        if plan_row is None:
            continue
        urns = plan_row[0] or []
        entity_ids = await _resolve_urns_to_entity_ids(session, list(urns))
        for eid in entity_ids:
            try:
                cur = (
                    await session.execute(
                        text("SELECT confidence FROM entities WHERE id = :id"),
                        {"id": eid},
                    )
                ).scalar_one_or_none()
                if cur is None:
                    continue
                proposed = max(Decimal("0.0"), Decimal(str(cur)) - Decimal("0.10"))
                await _learning.record_delta(
                    session,
                    target_kind="entity",
                    target_id=eid,
                    proposed_confidence=proposed,
                    reason="correction_retraction",
                    task_id=task_id,
                    plan_id=plan_id,
                    actor="cascade",
                )
                result.confidence_deltas_written += 1
            except Exception:
                logger.exception(
                    "failed to record delta for entity %s during cascade",
                    eid,
                )

    return result


# ── Helpers ─────────────────────────────────────────────────────────


async def _resolve_urns_to_entity_ids(
    session: AsyncSession,
    urns: list[str],
) -> list[str]:
    if not urns:
        return []
    rows = (
        await session.execute(
            text("SELECT id::text FROM entities WHERE urn = ANY(:urns)"),
            {"urns": urns},
        )
    ).all()
    return [r[0] for r in rows]
