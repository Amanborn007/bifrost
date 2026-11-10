"""Research-run state-machine helpers.

Wraps the ``research_runs`` table for the coordinator + API. Provides
the minimum needed to (a) start a run, (b) advance status as phases
complete, (c) terminate cleanly with done/failed, and (d) look up
existing runs for in-flight detection and idempotent retries.

The coordinator imports this module to write state at phase
boundaries; the conversations API imports it to dedup POST /messages
and to power ``GET /conversations/{id}/research-status``.

All helpers take a session_factory (``async_sessionmaker``) and open
their own short-lived session. This keeps the coordinator free of
DB transaction boilerplate, and means each phase write commits
independently — so a crash mid-run still leaves a partial trail of
truth in the DB.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.storage.models import ResearchRun
from bifrost.storage.sql_util import affected_rows

logger = logging.getLogger(__name__)


RunStatus = Literal[
    "pending",
    "scoping",
    "fanout",
    "synthesizing",
    "done",
    "failed",
    "cancelled",
]


# In-flight statuses — a run in any of these blocks a new POST and is
# what the frontend reattaches to on page refresh.
IN_FLIGHT_STATUSES: tuple[RunStatus, ...] = (
    "pending",
    "scoping",
    "fanout",
    "synthesizing",
)

# How long an idempotency_key remains valid for cached-answer return.
# Older keys are ignored; a fresh run is allowed.
IDEMPOTENCY_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class RunRecord:
    """Read-side projection of a research_runs row."""

    id: _uuid.UUID
    conversation_id: _uuid.UUID
    turn_id: int
    status: RunStatus
    phase_data: dict[str, Any]
    final_answer_text: str | None
    total_cost_usd: float
    saved_cost_usd: float
    started_at: datetime
    finished_at: datetime | None
    error_message: str | None
    idempotency_key: str | None

    @classmethod
    def from_row(cls, row: ResearchRun) -> RunRecord:
        return cls(
            id=row.id,
            conversation_id=row.conversation_id,
            turn_id=row.turn_id,
            status=row.status,  # type: ignore[arg-type]
            phase_data=dict(row.phase_data or {}),
            final_answer_text=row.final_answer_text,
            total_cost_usd=float(row.total_cost_usd or 0),
            saved_cost_usd=float(row.saved_cost_usd or 0),
            started_at=row.started_at,
            finished_at=row.finished_at,
            error_message=row.error_message,
            idempotency_key=row.idempotency_key,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "conversation_id": str(self.conversation_id),
            "turn_id": self.turn_id,
            "status": self.status,
            "phase_data": self.phase_data,
            "final_answer_text": self.final_answer_text,
            "total_cost_usd": self.total_cost_usd,
            "saved_cost_usd": self.saved_cost_usd,
            "started_at": self.started_at.isoformat(),
            "finished_at": (self.finished_at.isoformat() if self.finished_at else None),
            "error_message": self.error_message,
            "idempotency_key": self.idempotency_key,
        }


# ── Lookups ────────────────────────────────────────────────────────────


async def find_in_flight_run(
    session: AsyncSession,
    conversation_id: _uuid.UUID,
) -> RunRecord | None:
    """Return the most recent in-flight run for this conversation, if
    any. Used by POST /messages to 409 a duplicate submit, and by
    GET /research-status to power the page-refresh reattach."""
    stmt = (
        select(ResearchRun)
        .where(ResearchRun.conversation_id == conversation_id)
        .where(ResearchRun.status.in_(IN_FLIGHT_STATUSES))
        .order_by(ResearchRun.started_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return RunRecord.from_row(row) if row else None


async def find_latest_run(
    session: AsyncSession,
    conversation_id: _uuid.UUID,
) -> RunRecord | None:
    """Return the most recent run for this conversation regardless of
    status. Powers the page-mount status query when no in-flight
    exists — lets the frontend show a 'last attempt failed' banner
    instead of silently displaying nothing."""
    stmt = (
        select(ResearchRun)
        .where(ResearchRun.conversation_id == conversation_id)
        .order_by(ResearchRun.started_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return RunRecord.from_row(row) if row else None


async def find_by_idempotency(
    session: AsyncSession,
    conversation_id: _uuid.UUID,
    idempotency_key: str,
) -> RunRecord | None:
    """Return the most recent done run with this idempotency_key
    within the TTL window. Lets POST /messages return the cached
    answer for a retry without spawning a fresh research run."""
    cutoff = datetime.now(UTC) - IDEMPOTENCY_TTL
    stmt = (
        select(ResearchRun)
        .where(ResearchRun.conversation_id == conversation_id)
        .where(ResearchRun.idempotency_key == idempotency_key)
        .where(ResearchRun.status == "done")
        .where(ResearchRun.started_at >= cutoff)
        .order_by(ResearchRun.started_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return RunRecord.from_row(row) if row else None


# ── Mutations ──────────────────────────────────────────────────────────


async def start_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    conversation_id: _uuid.UUID,
    turn_id: int,
    idempotency_key: str | None = None,
) -> _uuid.UUID:
    """Insert a fresh ``pending`` row and return its id.

    Raises if a uniqueness collision occurs on (conversation_id,
    turn_id) — caller should treat that as 'a run already exists for
    this turn' and resume that one instead. (The conversations API
    handles this via find_in_flight_run before calling start_run.)
    """
    async with session_factory() as session:
        run = ResearchRun(
            conversation_id=conversation_id,
            turn_id=turn_id,
            idempotency_key=idempotency_key,
            status="pending",
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run.id


async def mark_phase(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: _uuid.UUID,
    phase: RunStatus,
    *,
    phase_data_patch: dict[str, Any] | None = None,
) -> None:
    """Advance a run's status. Intended for non-terminal phases:
    scoping → fanout → synthesizing. Use ``mark_done`` /
    ``mark_failed`` / ``mark_cancelled`` for terminal transitions."""
    if phase in ("done", "failed", "cancelled"):
        raise ValueError(
            f"mark_phase is for non-terminal phases; use mark_{phase} for {phase}",
        )
    async with session_factory() as session:
        if phase_data_patch:
            existing = (
                await session.execute(
                    select(ResearchRun.phase_data).where(ResearchRun.id == run_id),
                )
            ).scalar_one_or_none()
            merged = {**(existing or {}), **phase_data_patch}
            stmt = (
                update(ResearchRun)
                .where(ResearchRun.id == run_id)
                .values(status=phase, phase_data=merged)
            )
        else:
            stmt = update(ResearchRun).where(ResearchRun.id == run_id).values(status=phase)
        await session.execute(stmt)
        await session.commit()


async def mark_done(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: _uuid.UUID,
    *,
    final_answer_text: str | None = None,
    total_cost_usd: float = 0.0,
    saved_cost_usd: float = 0.0,
    phase_data_patch: dict[str, Any] | None = None,
) -> None:
    """Terminal — research turn finished successfully."""
    async with session_factory() as session:
        if phase_data_patch:
            existing = (
                await session.execute(
                    select(ResearchRun.phase_data).where(ResearchRun.id == run_id),
                )
            ).scalar_one_or_none()
            merged = {**(existing or {}), **phase_data_patch}
        else:
            merged = None
        values: dict[str, Any] = {
            "status": "done",
            "finished_at": datetime.now(UTC),
            "total_cost_usd": total_cost_usd,
            "saved_cost_usd": saved_cost_usd,
        }
        if final_answer_text is not None:
            values["final_answer_text"] = final_answer_text
        if merged is not None:
            values["phase_data"] = merged
        await session.execute(
            update(ResearchRun).where(ResearchRun.id == run_id).values(**values),
        )
        await session.commit()


async def mark_failed(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: _uuid.UUID,
    *,
    error_message: str,
    total_cost_usd: float = 0.0,
    saved_cost_usd: float = 0.0,
) -> None:
    """Terminal — research turn errored. Caller passes the
    user-actionable error string (synth failure, no usable repos, etc.)."""
    async with session_factory() as session:
        await session.execute(
            update(ResearchRun)
            .where(ResearchRun.id == run_id)
            .values(
                status="failed",
                error_message=error_message[:2000],
                finished_at=datetime.now(UTC),
                total_cost_usd=total_cost_usd,
                saved_cost_usd=saved_cost_usd,
            ),
        )
        await session.commit()


async def mark_cancelled(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: _uuid.UUID,
) -> bool:
    """Terminal — user cancelled the run via the UI.

    Conditional on the row still being in an in-flight status: a cancel
    that races a natural ``done``/``failed`` (the worker won) must not
    overwrite the real terminal state. Returns True if the row was
    transitioned to ``cancelled``, False if it was already terminal
    (or the id doesn't exist).
    """
    async with session_factory() as session:
        result = await session.execute(
            update(ResearchRun)
            .where(ResearchRun.id == run_id)
            .where(ResearchRun.status.in_(IN_FLIGHT_STATUSES))
            .values(
                status="cancelled",
                finished_at=datetime.now(UTC),
            ),
        )
        await session.commit()
        return affected_rows(result) > 0


async def reclaim_orphaned_runs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    older_than: timedelta = timedelta(minutes=10),
) -> int:
    """Mark stuck-in-flight rows older than ``older_than`` as failed.

    Called once at API startup so a backend that crashed mid-run
    doesn't leave permanent zombies that block all future POSTs to
    that conversation. ``older_than`` defaults to 10min — anything
    actually alive would have either finished or written a phase
    update by then.

    Returns the count of rows reclaimed.
    """
    cutoff = datetime.now(UTC) - older_than
    async with session_factory() as session:
        result = await session.execute(
            update(ResearchRun)
            .where(ResearchRun.status.in_(IN_FLIGHT_STATUSES))
            .where(ResearchRun.started_at < cutoff)
            .values(
                status="failed",
                error_message="Orphaned run reclaimed at API startup.",
                finished_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False),
        )
        await session.commit()
        return affected_rows(result)
