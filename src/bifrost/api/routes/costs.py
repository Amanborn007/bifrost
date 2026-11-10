"""Cost endpoint — real $ tracking via llm_cost_log.

Aggregates across task, model, phase, repo, and time. Provides the raw
data the Spend page needs to stop lying to users.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from bifrost.api.deps import DB
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.storage.models import LLMCostLog, Repo

router = APIRouter(prefix="/costs", tags=["costs"])


# ── Schemas ──────────────────────────────────────────────────────────


class ByRow(BaseModel):
    key: str
    label: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    calls: int


class DailyPoint(BaseModel):
    date: str  # YYYY-MM-DD
    cost_usd: float


class CacheSavings(BaseModel):
    """LLM response-cache impact: avoided cost from exact re-hits."""

    hits: int
    saved_usd: float


class CostSummary(BaseModel):
    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int
    total_calls: int
    by_model: list[ByRow]
    by_phase: list[ByRow]
    by_repo: list[ByRow]
    last_7_days: list[DailyPoint]
    cache_savings: CacheSavings
    window: Literal["7d", "30d", "all"]
    as_of: datetime


# Cache-hit rows (logged by the LLM response cache) are EXCLUDED from spend
# aggregations and surfaced separately as savings.
_CACHE_HIT_REASON = "llm_cache_hit"


# ── Route ────────────────────────────────────────────────────────────


@router.get("/summary", response_model=BifrostEnvelope[CostSummary])
async def cost_summary(
    session: DB,
    window: Literal["7d", "30d", "all"] = "30d",
) -> BifrostEnvelope[CostSummary]:
    """Return aggregated LLM cost across models, phases, repos, and time."""
    now = datetime.now(UTC)
    # The llm_cost_log.call_timestamp column is TIMESTAMP WITHOUT TIME ZONE.
    # Strip the tz so comparisons work against asyncpg's expected type.
    now_naive = now.replace(tzinfo=None)
    since: datetime | None = None
    if window == "7d":
        since = now_naive - timedelta(days=7)
    elif window == "30d":
        since = now_naive - timedelta(days=30)

    # Cache-hit rows are not spend — exclude from every cost aggregation
    # (surfaced separately as cache_savings below).
    _excl = LLMCostLog.skip_reason.is_distinct_from(_CACHE_HIT_REASON)

    base = select(LLMCostLog)
    if since is not None:
        base = base.where(LLMCostLog.call_timestamp >= since)

    # Totals
    totals_stmt = select(
        func.coalesce(func.sum(LLMCostLog.estimated_cost_usd), 0.0),
        func.coalesce(func.sum(LLMCostLog.input_tokens), 0),
        func.coalesce(func.sum(LLMCostLog.output_tokens), 0),
        func.count(LLMCostLog.id),
    )
    if since is not None:
        totals_stmt = totals_stmt.where(LLMCostLog.call_timestamp >= since)
    totals_stmt = totals_stmt.where(_excl)
    totals = (await session.execute(totals_stmt)).one()
    total_cost, total_in, total_out, total_calls = totals

    # By model
    by_model_stmt = (
        select(
            LLMCostLog.model,
            func.sum(LLMCostLog.estimated_cost_usd),
            func.sum(LLMCostLog.input_tokens),
            func.sum(LLMCostLog.output_tokens),
            func.count(LLMCostLog.id),
        )
        .group_by(LLMCostLog.model)
        .order_by(func.sum(LLMCostLog.estimated_cost_usd).desc())
    )
    if since is not None:
        by_model_stmt = by_model_stmt.where(LLMCostLog.call_timestamp >= since)
    by_model_stmt = by_model_stmt.where(_excl)
    by_model = [
        ByRow(
            key=row[0],
            label=row[0],
            cost_usd=float(row[1] or 0.0),
            input_tokens=int(row[2] or 0),
            output_tokens=int(row[3] or 0),
            calls=int(row[4] or 0),
        )
        for row in (await session.execute(by_model_stmt)).all()
    ]

    # By phase
    by_phase_stmt = (
        select(
            LLMCostLog.phase,
            func.sum(LLMCostLog.estimated_cost_usd),
            func.sum(LLMCostLog.input_tokens),
            func.sum(LLMCostLog.output_tokens),
            func.count(LLMCostLog.id),
        )
        .group_by(LLMCostLog.phase)
        .order_by(LLMCostLog.phase)
    )
    if since is not None:
        by_phase_stmt = by_phase_stmt.where(LLMCostLog.call_timestamp >= since)
    by_phase_stmt = by_phase_stmt.where(_excl)
    by_phase = [
        ByRow(
            key=row[0],
            label=_phase_label(row[0]),
            cost_usd=float(row[1] or 0.0),
            input_tokens=int(row[2] or 0),
            output_tokens=int(row[3] or 0),
            calls=int(row[4] or 0),
        )
        for row in (await session.execute(by_phase_stmt)).all()
    ]

    # By repo — join to get names
    by_repo_stmt = (
        select(
            LLMCostLog.repo_id,
            Repo.name,
            func.sum(LLMCostLog.estimated_cost_usd),
            func.sum(LLMCostLog.input_tokens),
            func.sum(LLMCostLog.output_tokens),
            func.count(LLMCostLog.id),
        )
        .join(Repo, Repo.id == LLMCostLog.repo_id, isouter=True)
        .group_by(LLMCostLog.repo_id, Repo.name)
        .order_by(func.sum(LLMCostLog.estimated_cost_usd).desc())
    )
    if since is not None:
        by_repo_stmt = by_repo_stmt.where(LLMCostLog.call_timestamp >= since)
    by_repo_stmt = by_repo_stmt.where(_excl)
    by_repo = [
        ByRow(
            key=str(row[0]) if row[0] else "(unscoped)",
            label=row[1] or "(unscoped)",
            cost_usd=float(row[2] or 0.0),
            input_tokens=int(row[3] or 0),
            output_tokens=int(row[4] or 0),
            calls=int(row[5] or 0),
        )
        for row in (await session.execute(by_repo_stmt)).all()
    ]

    # Last 7 days — day-bucketed sum
    seven_ago = now_naive - timedelta(days=6)
    day_stmt = (
        select(
            func.date_trunc("day", LLMCostLog.call_timestamp).label("day"),
            func.sum(LLMCostLog.estimated_cost_usd),
        )
        .where(LLMCostLog.call_timestamp >= seven_ago)
        .where(_excl)
        .group_by("day")
        .order_by("day")
    )
    day_rows = (await session.execute(day_stmt)).all()
    raw_series: dict[str, float] = {}
    for row in day_rows:
        day_dt = row[0]
        if day_dt is not None:
            day_key = day_dt.date().isoformat()
            raw_series[day_key] = float(row[1] or 0.0)
    # Fill gaps so the series is always 7 points
    last_7: list[DailyPoint] = []
    for i in range(6, -1, -1):
        d = (now - timedelta(days=i)).date()
        last_7.append(
            DailyPoint(date=d.isoformat(), cost_usd=raw_series.get(d.isoformat(), 0.0)),
        )

    # Cache savings — avoided cost from response-cache hits (windowed).
    savings_stmt = select(
        func.count(LLMCostLog.id),
        func.coalesce(func.sum(LLMCostLog.estimated_cost_usd), 0.0),
    ).where(LLMCostLog.skip_reason == _CACHE_HIT_REASON)
    if since is not None:
        savings_stmt = savings_stmt.where(LLMCostLog.call_timestamp >= since)
    s_hits, s_saved = (await session.execute(savings_stmt)).one()

    return BifrostEnvelope(
        data=CostSummary(
            total_cost_usd=float(total_cost or 0.0),
            total_input_tokens=int(total_in or 0),
            total_output_tokens=int(total_out or 0),
            total_calls=int(total_calls or 0),
            by_model=by_model,
            by_phase=by_phase,
            by_repo=by_repo,
            last_7_days=last_7,
            cache_savings=CacheSavings(hits=int(s_hits or 0), saved_usd=float(s_saved or 0.0)),
            window=window,
            as_of=now,
        ),
    )


# ── Helpers ──────────────────────────────────────────────────────────


def _phase_label(phase: str) -> str:
    return {
        "A": "Triage",
        "D": "Components",
        "E": "Flows",
        "F": "Dependencies",
        "G": "Relations",
        "H": "Knowledge index",
        "I": "Confidence",
        "chat": "Chat",
    }.get(phase, phase)
