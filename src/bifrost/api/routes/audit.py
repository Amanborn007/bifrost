"""GET /api/v1/admin/audit -- read the security-event audit log.

S11 section 17.3 mandates an audit-log query API. V1 surface:
  * Filter by event_type prefix, actor, time window.
  * Cursor / limit pagination (default 100, max 500).
  * Returns rows in reverse-chronological order so the most recent
    events surface first.

V2 (deferred): structured search by metadata fields, real cursor pagination,
streaming SSE for live tail.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from bifrost.api.deps import DB, AdminOnly
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.storage.models import AuditLog

# Mounted via admin_router_top, which also applies the admin gate; the
# dep is repeated here so this surface stays gated if the wrapper changes.
router = APIRouter(
    prefix="/admin/audit",
    tags=["admin", "audit"],
    dependencies=[AdminOnly],
)


class AuditEntry(BaseModel):
    id: str
    timestamp: datetime
    event_type: str
    actor: str | None
    resource: str | None
    method: str | None
    status_code: int | None
    correlation_id: str | None
    source_ip: str | None
    metadata: dict[str, Any]


class AuditPage(BaseModel):
    entries: list[AuditEntry]
    total_returned: int


@router.get("", response_model=BifrostEnvelope[AuditPage])
async def list_audit_events(
    session: DB,
    event_type_prefix: str | None = Query(
        default=None,
        description=("Filter by dotted-namespace prefix, e.g. 'admin.' or 'admin.access.denied'."),
    ),
    actor: str | None = Query(default=None),
    since: Annotated[
        datetime | None,
        Query(description="ISO timestamp; only return rows >= this time."),
    ] = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> BifrostEnvelope[AuditPage]:
    """Page through the audit log, most recent first."""
    stmt = select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit)
    if event_type_prefix:
        stmt = stmt.where(AuditLog.event_type.startswith(event_type_prefix))
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
    if since:
        stmt = stmt.where(AuditLog.timestamp >= since)

    rows = (await session.execute(stmt)).scalars().all()
    entries = [
        AuditEntry(
            id=str(r.id),
            timestamp=r.timestamp,
            event_type=r.event_type,
            actor=r.actor,
            resource=r.resource,
            method=r.method,
            status_code=r.status_code,
            correlation_id=r.correlation_id,
            source_ip=r.source_ip,
            metadata=r.audit_metadata,
        )
        for r in rows
    ]
    return BifrostEnvelope(
        data=AuditPage(entries=entries, total_returned=len(entries)),
    )
