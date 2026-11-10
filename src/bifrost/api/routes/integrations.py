"""GET /api/v1/integrations/health — aggregate sync status for the frontend.

Each data panel in the UI needs two signals:

  * "last synced X ago" — derived from ExternalSyncLog.last_synced_at
  * "not configured" / error indicator — derived from row presence + error column

This endpoint answers both in a single round-trip.  It is intentionally
read-only and carries no caching layer; the query is cheap (two aggregates
against small tables) and the frontend polls on a short interval, so staleness
from a cache would be misleading in exactly the cases the UI is trying to
surface.

Status derivation per source:

  not_configured  — no rows exist in external_sync_log for that source
  ok              — has rows + last_synced_at < 2 h ago + zero error rows
  degraded        — has rows + (last_synced_at 2–24 h ago OR some errors)
  error           — has rows + last_synced_at > 24 h ago OR every row has an error
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from fastapi import APIRouter, status
from pydantic import BaseModel
from sqlalchemy import func, select

from bifrost.api.deps import Repo
from bifrost.api.schemas.envelope import BifrostEnvelope, EnvelopeMeta
from bifrost.storage.models import ExternalSyncLog, RepoFreshness

router = APIRouter(prefix="/integrations", tags=["integrations"])

log = structlog.get_logger(__name__)

# ── Response schema ───────────────────────────────────────────────────────────

SourceStatus = Literal["ok", "degraded", "not_configured", "error"]

_FRESH_THRESHOLD = timedelta(hours=2)
_STALE_THRESHOLD = timedelta(hours=24)


class SourceHealth(BaseModel):
    """Health snapshot for a single external integration source."""

    configured: bool
    """True when at least one sync-log row exists for this source."""

    last_synced_at: datetime | None
    """Wall-clock time of the most recent successful (or attempted) sync."""

    items_indexed_total: int
    """Sum of items_indexed across all repos for this source."""

    error_count: int
    """Number of rows where the error column IS NOT NULL."""

    status: SourceStatus


class ExtractionHealth(BaseModel):
    """Aggregate extraction freshness across all registered repos."""

    repos_fresh: int
    repos_stale: int
    repos_failed: int
    repos_unknown: int
    last_indexed_at: datetime | None
    """Most recent last_indexed_at value across all repos."""


class IntegrationsHealthResponse(BaseModel):
    jira: SourceHealth
    github: SourceHealth
    confluence: SourceHealth
    extraction: ExtractionHealth
    checked_at: datetime


# ── Status derivation ─────────────────────────────────────────────────────────


def _derive_status(
    row_count: int,
    last_synced_at: datetime | None,
    error_count: int,
    total_count: int,
    now: datetime,
) -> SourceStatus:
    """Pure function — no I/O.  Centralised so tests can target it directly."""
    if row_count == 0 or last_synced_at is None:
        return "not_configured"

    age = now - last_synced_at

    # All rows errored — treat as hard error regardless of recency.
    all_errored = total_count > 0 and error_count >= total_count

    if all_errored or age > _STALE_THRESHOLD:
        return "error"

    if age > _FRESH_THRESHOLD or error_count > 0:
        return "degraded"

    return "ok"


# ── Route ─────────────────────────────────────────────────────────────────────


@router.get(
    "/health",
    response_model=BifrostEnvelope[IntegrationsHealthResponse],
    status_code=status.HTTP_200_OK,
)
async def integrations_health(
    repo: Repo,
) -> BifrostEnvelope[IntegrationsHealthResponse]:
    """Return an aggregate sync-health snapshot across all integration sources.

    Two SQL queries — one aggregate over external_sync_log (per source), one
    aggregate over repo_freshness (per freshness_state).  No per-row fetches.
    """
    session = repo._session  # noqa: SLF001 -- intentional shared session

    now = datetime.now(UTC)

    # ── Query 1: external_sync_log ────────────────────────────────────────────
    # One pass: group by source, compute MAX(last_synced_at), SUM(items_indexed),
    # COUNT(error IS NOT NULL), and total row count.
    sync_stmt = select(
        ExternalSyncLog.source,
        func.count().label("row_count"),
        func.max(ExternalSyncLog.last_synced_at).label("last_synced_at"),
        func.coalesce(func.sum(ExternalSyncLog.items_indexed), 0).label("items_indexed_total"),
        func.count(ExternalSyncLog.error).label("error_count"),
    ).group_by(ExternalSyncLog.source)

    sync_result = await session.execute(sync_stmt)
    sync_rows = sync_result.all()

    # Build a lookup keyed by source name.
    sync_by_source: dict[str, tuple[int, datetime | None, int, int]] = {}
    for row in sync_rows:
        source = row.source
        last_at = row.last_synced_at
        # Ensure the datetime is timezone-aware (DB may return naive UTC).
        if last_at is not None and last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=UTC)
        sync_by_source[source] = (
            row.row_count,
            last_at,
            int(row.items_indexed_total),
            row.error_count,
        )

    log.debug(
        "integrations_health.sync_query_done",
        sources_found=list(sync_by_source.keys()),
    )

    def _build_source_health(source_name: str) -> SourceHealth:
        if source_name not in sync_by_source:
            return SourceHealth(
                configured=False,
                last_synced_at=None,
                items_indexed_total=0,
                error_count=0,
                status="not_configured",
            )
        row_count, last_synced_at, items_total, error_count = sync_by_source[source_name]
        derived_status = _derive_status(
            row_count=row_count,
            last_synced_at=last_synced_at,
            error_count=error_count,
            total_count=row_count,
            now=now,
        )
        return SourceHealth(
            configured=True,
            last_synced_at=last_synced_at,
            items_indexed_total=items_total,
            error_count=error_count,
            status=derived_status,
        )

    # ── Query 2: repo_freshness ───────────────────────────────────────────────
    # Collapse all rows into counts per freshness_state + a global MAX.
    freshness_stmt = select(
        RepoFreshness.freshness_state,
        func.count().label("cnt"),
        func.max(RepoFreshness.last_indexed_at).label("last_indexed_at"),
    ).group_by(RepoFreshness.freshness_state)

    freshness_result = await session.execute(freshness_stmt)
    freshness_rows = freshness_result.all()

    counts: dict[str, int] = {}
    latest_indexed_at: datetime | None = None

    for row in freshness_rows:
        counts[row.freshness_state] = row.cnt
        if row.last_indexed_at is not None:
            ts = row.last_indexed_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if latest_indexed_at is None or ts > latest_indexed_at:
                latest_indexed_at = ts

    log.debug(
        "integrations_health.freshness_query_done",
        freshness_counts=counts,
    )

    # "stale" bucket covers both stale and very_stale labels.
    repos_stale = counts.get("stale", 0) + counts.get("very_stale", 0)

    extraction = ExtractionHealth(
        repos_fresh=counts.get("fresh", 0),
        repos_stale=repos_stale,
        repos_failed=counts.get("failed", 0),
        repos_unknown=counts.get("unknown", 0),
        last_indexed_at=latest_indexed_at,
    )

    payload = IntegrationsHealthResponse(
        jira=_build_source_health("jira"),
        github=_build_source_health("github"),
        confluence=_build_source_health("confluence"),
        extraction=extraction,
        checked_at=now,
    )

    # Derive a coarse overall freshness string for the envelope meta —
    # "fresh" only when all three sources are ok and extraction has no failures.
    any_error = any(s.status == "error" for s in (payload.jira, payload.github, payload.confluence))
    any_degraded = any(
        s.status == "degraded" for s in (payload.jira, payload.github, payload.confluence)
    )
    if any_error or extraction.repos_failed > 0:
        freshness_label = "degraded"
    elif any_degraded or extraction.repos_stale > 0:
        freshness_label = "stale"
    else:
        freshness_label = "fresh"

    return BifrostEnvelope(
        data=payload,
        meta=EnvelopeMeta(
            data_source="extraction",
            cache_status="live",
            freshness=freshness_label,
            evidence_summary=(
                f"jira={payload.jira.status} "
                f"github={payload.github.status} "
                f"confluence={payload.confluence.status}; "
                f"extraction fresh={extraction.repos_fresh} "
                f"stale={extraction.repos_stale} "
                f"failed={extraction.repos_failed}"
            ),
        ),
    )
