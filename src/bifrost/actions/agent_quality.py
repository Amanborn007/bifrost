"""Agent-quality aggregation queries.

Operator-facing signals for spotting drift in the action layer's
agents. All queries are cheap pure reads — no locks, no mutations —
so the snapshot can be polled from the admin UI every few seconds
without guilt.

The returned payload is a single dict so the frontend renders with
one fetch instead of N. Sections:

* ``verify_distribution`` — totals + 7-day-window ratios for
  ``task_verify_items.status``. High ``skipped`` often means the
  verifier is being too timid; high ``fail`` means the plan agent
  is producing tasks that don't actually satisfy the done criterion.
* ``plan_outcomes`` — status histogram across every ``task_plans``
  row (completed / failed / rejected / executing / pending).
  Failure rate per wave helps spot plan agent drift.
* ``agent_metrics`` — per-agent count, total cost, total tokens,
  avg duration pulled from ``ask_backs``. Tracks whether the
  plan/discussion/verify/debug agents are getting slower or more
  expensive over time.
* ``assumption_tiers`` — distribution of active assumption tiers +
  source_types. Shift toward T5 ``llm_guess`` == discussion agent
  is hallucinating more. Shift toward T1 ``user_stated`` ==
  clarifications are working.
* ``plan_readiness`` — how often the plan agent's
  ``ready_to_execute=true`` actually ends with every plan
  completed. Drift metric for planner optimism.
* ``quick_mode`` — quick-mode invocation count + fallback rate.
* ``tasks_by_state`` — current task lifecycle histogram.

Everything is bounded by the last N days (default 30) so dead
sessions from long ago don't mask recent drift.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


_DEFAULT_WINDOW_DAYS = 30
_TREND_WINDOW_DAYS = 7


@dataclass
class AgentQualitySnapshot:
    window_days: int
    trend_window_days: int
    verify_distribution: dict[str, Any] = field(default_factory=dict)
    plan_outcomes: dict[str, Any] = field(default_factory=dict)
    agent_metrics: list[dict[str, Any]] = field(default_factory=list)
    assumption_tiers: dict[str, Any] = field(default_factory=dict)
    plan_readiness: dict[str, Any] = field(default_factory=dict)
    quick_mode: dict[str, Any] = field(default_factory=dict)
    tasks_by_state: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Query fragments ────────────────────────────────────────────────


_VERIFY_DISTRIBUTION_SQL = """
    WITH all_items AS (
        SELECT v.status, v.answered_at, v.phase_id
        FROM task_verify_items v
        JOIN task_phases p ON p.id = v.phase_id
        JOIN tasks b ON b.id = p.task_id
        WHERE b.created_at >= NOW() - make_interval(days => :window)
    )
    SELECT
        status,
        COUNT(*) AS total,
        COUNT(*) FILTER (
            WHERE answered_at >= NOW() - make_interval(days => :trend)
        ) AS recent
    FROM all_items
    GROUP BY status
"""

_PLAN_OUTCOMES_SQL = """
    SELECT p.status, COUNT(*) AS total
    FROM task_plans p
    JOIN task_phases ph ON ph.id = p.phase_id
    JOIN tasks b ON b.id = ph.task_id
    WHERE b.created_at >= NOW() - make_interval(days => :window)
    GROUP BY p.status
"""

_AGENT_METRICS_SQL = """
    SELECT
        agent_kind,
        COUNT(*) AS n,
        COALESCE(SUM(research_cost), 0) AS cost_usd,
        COALESCE(SUM(research_tokens), 0) AS tokens,
        COALESCE(AVG(duration_ms), 0) AS avg_duration_ms,
        COALESCE(
            AVG(CASE WHEN answer_confidence IS NOT NULL
                THEN answer_confidence END),
            0
        ) AS avg_confidence,
        COUNT(*) FILTER (WHERE answered_from = 'budget_exceeded') AS budget_hits
    FROM ask_backs
    WHERE created_at >= NOW() - make_interval(days => :window)
    GROUP BY agent_kind
    ORDER BY n DESC
"""

_ASSUMPTION_TIERS_SQL = """
    SELECT tier, source_type, COUNT(*) AS n
    FROM task_assumptions a
    JOIN task_artifacts art ON art.id = a.artifact_id
    JOIN task_phases p ON p.id = art.phase_id
    JOIN tasks b ON b.id = p.task_id
    WHERE a.state = 'active'
      AND b.created_at >= NOW() - make_interval(days => :window)
    GROUP BY tier, source_type
    ORDER BY tier, source_type
"""

# Plan readiness: did the plan agent say ready_to_execute=true AND
# every plan for that phase actually complete?
_PLAN_READINESS_SQL = """
    WITH plan_artifacts AS (
        SELECT
            a.id AS artifact_id,
            a.phase_id,
            COALESCE((a.parsed->>'ready_to_execute')::boolean, false) AS ready
        FROM task_artifacts a
        JOIN task_phases p ON p.id = a.phase_id
        JOIN tasks b ON b.id = p.task_id
        WHERE a.kind = 'plan'
          AND b.created_at >= NOW() - make_interval(days => :window)
    ),
    phase_plan_counts AS (
        SELECT
            p.phase_id,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE p.status = 'completed') AS completed,
            COUNT(*) FILTER (WHERE p.status = 'failed') AS failed
        FROM task_plans p
        JOIN plan_artifacts pa ON pa.phase_id = p.phase_id
        GROUP BY p.phase_id
    )
    SELECT
        pa.ready AS ready_to_execute,
        COUNT(*) AS n_phases,
        COUNT(*) FILTER (
            WHERE ppc.total IS NOT NULL
              AND ppc.completed = ppc.total
        ) AS n_all_completed,
        COUNT(*) FILTER (WHERE ppc.failed > 0) AS n_any_failed
    FROM plan_artifacts pa
    LEFT JOIN phase_plan_counts ppc ON ppc.phase_id = pa.phase_id
    GROUP BY pa.ready
"""

# Quick-mode: look for phases whose sole plan artifact has
# parsed->>'quick_mode' = 'true'.
_QUICK_MODE_SQL = """
    SELECT
        COUNT(*) AS invocations,
        COUNT(*) FILTER (
            WHERE b.state = 'success'
        ) AS successes,
        COUNT(*) FILTER (
            WHERE b.state = 'blocked'
        ) AS blocks,
        COUNT(*) FILTER (
            WHERE b.state = 'cancelled'
        ) AS cancelled
    FROM tasks b
    WHERE b.created_at >= NOW() - make_interval(days => :window)
      AND EXISTS (
          SELECT 1 FROM task_artifacts a
          JOIN task_phases p ON p.id = a.phase_id
          WHERE p.task_id = b.id
            AND a.kind = 'plan'
            AND (a.parsed->>'quick_mode') = 'true'
      )
"""

_TASKS_BY_STATE_SQL = """
    SELECT state, COUNT(*) AS n
    FROM tasks
    WHERE created_at >= NOW() - make_interval(days => :window)
    GROUP BY state
    ORDER BY n DESC
"""


# ── Public entry point ─────────────────────────────────────────────


async def snapshot(
    session: AsyncSession,
    *,
    window_days: int = _DEFAULT_WINDOW_DAYS,
    trend_window_days: int = _TREND_WINDOW_DAYS,
) -> AgentQualitySnapshot:
    """Return a single-shot operator snapshot."""
    snap = AgentQualitySnapshot(
        window_days=window_days,
        trend_window_days=trend_window_days,
    )

    # 1. Verify distribution
    rows = (
        await session.execute(
            text(_VERIFY_DISTRIBUTION_SQL),
            {"window": window_days, "trend": trend_window_days},
        )
    ).all()
    total_total = sum(int(r[1] or 0) for r in rows)
    total_recent = sum(int(r[2] or 0) for r in rows)
    snap.verify_distribution = {
        "total": total_total,
        "total_recent": total_recent,
        "by_status": [
            {
                "status": r[0],
                "n": int(r[1] or 0),
                "recent": int(r[2] or 0),
                "pct": (float(r[1] or 0) / total_total) if total_total else 0.0,
                "recent_pct": ((float(r[2] or 0) / total_recent) if total_recent else 0.0),
            }
            for r in rows
        ],
    }

    # 2. Plan outcomes
    rows = (
        await session.execute(
            text(_PLAN_OUTCOMES_SQL),
            {"window": window_days},
        )
    ).all()
    total = sum(int(r[1] or 0) for r in rows)
    snap.plan_outcomes = {
        "total": total,
        "by_status": [
            {
                "status": r[0],
                "n": int(r[1] or 0),
                "pct": (float(r[1] or 0) / total) if total else 0.0,
            }
            for r in rows
        ],
    }

    # 3. Agent metrics (per-kind)
    rows = (
        await session.execute(
            text(_AGENT_METRICS_SQL),
            {"window": window_days},
        )
    ).all()
    snap.agent_metrics = [
        {
            "agent_kind": r[0],
            "n": int(r[1] or 0),
            "cost_usd": float(r[2] or 0),
            "tokens": int(r[3] or 0),
            "avg_duration_ms": float(r[4] or 0),
            "avg_confidence": float(r[5] or 0),
            "budget_hits": int(r[6] or 0),
        }
        for r in rows
    ]

    # 4. Assumption tiers
    rows = (
        await session.execute(
            text(_ASSUMPTION_TIERS_SQL),
            {"window": window_days},
        )
    ).all()
    tier_totals: dict[int, int] = {}
    by_tier_source: list[dict[str, Any]] = []
    for tier, source_type, n in rows:
        tier_i = int(tier or 5)
        n_i = int(n or 0)
        tier_totals[tier_i] = tier_totals.get(tier_i, 0) + n_i
        by_tier_source.append({"tier": tier_i, "source_type": source_type, "n": n_i})
    total_assumptions = sum(tier_totals.values())
    snap.assumption_tiers = {
        "total_active": total_assumptions,
        "by_tier": [
            {
                "tier": t,
                "n": tier_totals.get(t, 0),
                "pct": ((tier_totals.get(t, 0) / total_assumptions) if total_assumptions else 0.0),
            }
            for t in sorted(tier_totals.keys())
        ],
        "by_tier_source": by_tier_source,
    }

    # 5. Plan readiness
    rows = (
        await session.execute(
            text(_PLAN_READINESS_SQL),
            {"window": window_days},
        )
    ).all()
    readiness: dict[str, dict[str, int]] = {
        "ready": {"n_phases": 0, "all_completed": 0, "any_failed": 0},
        "not_ready": {"n_phases": 0, "all_completed": 0, "any_failed": 0},
    }
    for ready, n_phases, n_all_completed, n_any_failed in rows:
        key = "ready" if ready else "not_ready"
        readiness[key]["n_phases"] = int(n_phases or 0)
        readiness[key]["all_completed"] = int(n_all_completed or 0)
        readiness[key]["any_failed"] = int(n_any_failed or 0)
    # Optimism score: of phases flagged ready_to_execute, what fraction
    # actually completed every plan without a failure?
    r_tot = readiness["ready"]["n_phases"]
    r_ok = readiness["ready"]["all_completed"]
    snap.plan_readiness = {
        "ready": readiness["ready"],
        "not_ready": readiness["not_ready"],
        "optimism_score": ((r_ok / r_tot) if r_tot else None),
    }

    # 6. Quick-mode
    row = (
        await session.execute(
            text(_QUICK_MODE_SQL),
            {"window": window_days},
        )
    ).first()
    invocations = int(row[0] or 0) if row else 0
    snap.quick_mode = {
        "invocations": invocations,
        "successes": int(row[1] or 0) if row else 0,
        "blocked": int(row[2] or 0) if row else 0,
        "cancelled": int(row[3] or 0) if row else 0,
        "success_rate": (float(row[1] or 0) / invocations if row and invocations else 0.0),
    }

    # 7. Tasks by state
    rows = (
        await session.execute(
            text(_TASKS_BY_STATE_SQL),
            {"window": window_days},
        )
    ).all()
    snap.tasks_by_state = {r[0]: int(r[1] or 0) for r in rows}

    return snap
