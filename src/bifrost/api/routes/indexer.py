"""Endpoints for populating the external-source cache.

POST /indexer/repos/{id}              — index Confluence + Jira for one repo
POST /indexer/all                      — index every registered repo (background)
GET  /indexer/status                   — show per-repo last-synced state
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from bifrost.api.deps import DB, AdminOnly, get_session_factory
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.cli.config import get_settings
from bifrost.research.external_indexer import (
    IndexResult,
    index_repo_external_sources,
)
from bifrost.storage.models import ExternalSyncLog, Repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/indexer", tags=["indexer"])


class IndexResponse(BaseModel):
    repo_name: str
    results: list[dict]


class StatusEntry(BaseModel):
    repo_id: str
    repo_name: str
    source: str
    last_synced_at: str | None
    items_indexed: int
    error: str | None


@router.post(
    "/repos/{repo_id}",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[IndexResponse],
)
async def index_one(
    repo_id: str,
    session: DB,
) -> BifrostEnvelope[IndexResponse]:
    """Synchronously index Confluence + Jira for this repo. Returns counts."""
    rid = uuid.UUID(repo_id)
    existing = (await session.execute(select(Repo).where(Repo.id == rid))).scalar_one_or_none()
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repo not found",
        )

    results: list[IndexResult] = await index_repo_external_sources(
        session_factory=get_session_factory(get_settings()),
        repo_id=repo_id,
    )

    return BifrostEnvelope(
        data=IndexResponse(
            repo_name=existing.name,
            results=[
                {
                    "source": r.source,
                    "items_indexed": r.items_indexed,
                    "error": r.error,
                }
                for r in results
            ],
        ),
    )


@router.post("/all", dependencies=[AdminOnly], response_model=BifrostEnvelope[dict])
async def index_all() -> BifrostEnvelope[dict]:
    """Kick off indexing for every registered repo, in the background.

    Returns immediately with the count queued. Check /indexer/status for
    per-repo progress.
    """
    sf = get_session_factory(get_settings())
    async with sf() as s:
        rows = (await s.execute(select(Repo.id, Repo.name))).all()
    repos = [(str(r.id), r.name) for r in rows]

    async def _run_all() -> None:
        # Bounded concurrency — each indexer spawns a claude -p + MCP round-trips.
        sem = asyncio.Semaphore(3)

        async def _one(rid: str, name: str) -> None:
            async with sem:
                try:
                    await index_repo_external_sources(
                        session_factory=sf,
                        repo_id=rid,
                    )
                except Exception:
                    logger.exception("index_all: %s failed", name)

        await asyncio.gather(*(_one(r, n) for r, n in repos))

    asyncio.create_task(_run_all())
    return BifrostEnvelope(
        data={"queued": len(repos), "concurrency": 3, "status": "running_in_background"},
    )


@router.get("/status", response_model=BifrostEnvelope[list[StatusEntry]])
async def indexer_status(session: DB) -> BifrostEnvelope[list[StatusEntry]]:
    """Return the last sync state for every (repo, source) pair."""
    stmt = (
        select(ExternalSyncLog, Repo)
        .join(Repo, Repo.id == ExternalSyncLog.repo_id)
        .order_by(Repo.name, ExternalSyncLog.source)
    )
    rows = (await session.execute(stmt)).all()
    return BifrostEnvelope(
        data=[
            StatusEntry(
                repo_id=str(r.Repo.id),
                repo_name=r.Repo.name,
                source=r.ExternalSyncLog.source,
                last_synced_at=(
                    r.ExternalSyncLog.last_synced_at.isoformat()
                    if r.ExternalSyncLog.last_synced_at
                    else None
                ),
                items_indexed=int(r.ExternalSyncLog.items_indexed or 0),
                error=r.ExternalSyncLog.error,
            )
            for r in rows
        ],
    )
