"""Org-wide repo catalog endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from bifrost.api.deps import DB, AdminOnly, get_session_factory
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.cli.config import get_settings
from bifrost.manifest.graph_apply import spawn_background
from bifrost.research.org_catalog import NO_ORGS_ERROR, sync_org_catalog
from bifrost.storage.models import OrgRepo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/catalog", tags=["catalog"])


class CatalogSummary(BaseModel):
    total_repos: int
    archived: int
    forks: int
    with_squad: int
    squads: dict[str, int]


class CatalogRow(BaseModel):
    owner: str
    name: str
    url: str
    description: str | None
    primary_language: str | None
    squad: str | None
    service_tier: str | None
    archived: bool
    is_fork: bool
    stars: int
    pushed_at: str | None
    registered: bool


class CatalogSyncResponse(BaseModel):
    org: str
    total_fetched: int
    manifest_hits: int
    errors: list[str]
    status: str = "complete"


@router.get("/summary", response_model=BifrostEnvelope[CatalogSummary])
async def catalog_summary(session: DB) -> BifrostEnvelope[CatalogSummary]:
    total = (await session.execute(select(func.count(OrgRepo.id)))).scalar_one()
    archived = (
        await session.execute(
            select(func.count(OrgRepo.id)).where(OrgRepo.archived.is_(True)),
        )
    ).scalar_one()
    forks = (
        await session.execute(
            select(func.count(OrgRepo.id)).where(OrgRepo.is_fork.is_(True)),
        )
    ).scalar_one()
    with_squad = (
        await session.execute(
            select(func.count(OrgRepo.id)).where(OrgRepo.squad.isnot(None)),
        )
    ).scalar_one()
    squad_rows = (
        await session.execute(
            select(OrgRepo.squad, func.count(OrgRepo.id))
            .where(OrgRepo.squad.isnot(None))
            .group_by(OrgRepo.squad)
            .order_by(desc(func.count(OrgRepo.id))),
        )
    ).all()
    return BifrostEnvelope(
        data=CatalogSummary(
            total_repos=int(total or 0),
            archived=int(archived or 0),
            forks=int(forks or 0),
            with_squad=int(with_squad or 0),
            squads={row[0]: int(row[1]) for row in squad_rows if row[0]},
        ),
    )


@router.get("", response_model=BifrostEnvelope[list[CatalogRow]])
async def catalog_list(
    session: DB,
    squad: str | None = None,
    search: str | None = None,
    limit: int = 200,
) -> BifrostEnvelope[list[CatalogRow]]:
    """List catalog rows, newest-pushed first."""
    stmt = select(OrgRepo)
    if squad:
        stmt = stmt.where(OrgRepo.squad == squad)
    if search:
        like = f"%{search.lower()}%"
        stmt = stmt.where(
            func.lower(OrgRepo.name).like(like)
            | func.lower(func.coalesce(OrgRepo.description, "")).like(like),
        )
    stmt = stmt.order_by(desc(OrgRepo.pushed_at)).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return BifrostEnvelope(
        data=[
            CatalogRow(
                owner=r.owner,
                name=r.name,
                url=r.url,
                description=r.description,
                primary_language=r.primary_language,
                squad=r.squad,
                service_tier=r.service_tier,
                archived=r.archived,
                is_fork=r.is_fork,
                stars=r.stars,
                pushed_at=r.pushed_at.isoformat() if r.pushed_at else None,
                registered=r.registered_repo_id is not None,
            )
            for r in rows
        ],
    )


@router.post("/sync", dependencies=[AdminOnly], response_model=BifrostEnvelope[CatalogSyncResponse])
async def trigger_catalog_sync(
    org: str | None = None,
    background: bool = True,
) -> BifrostEnvelope[CatalogSyncResponse]:
    """Pull every repo of the configured orgs (or just ``org``) into the catalog.

    ``background=true`` (default) returns immediately and runs the sync
    in a bg task. ``background=false`` blocks until complete — useful
    for CI or a manual "index now" button.
    """
    settings = get_settings()
    sf = get_session_factory(settings)

    if org is None and not settings.sources.github.orgs:
        return BifrostEnvelope(
            data=CatalogSyncResponse(
                org="",
                total_fetched=0,
                manifest_hits=0,
                errors=[NO_ORGS_ERROR],
                status="error",
            ),
        )

    if background:

        async def _run() -> None:
            result = await sync_org_catalog(session_factory=sf, org=org, settings=settings)
            if result.errors:
                logger.error("catalog sync finished with errors: %s", result.errors)

        spawn_background(_run(), what="catalog sync")
        return BifrostEnvelope(
            data=CatalogSyncResponse(
                org=org or ",".join(settings.sources.github.orgs),
                total_fetched=0,
                manifest_hits=0,
                errors=[],
                status="running_in_background",
            ),
        )

    result = await sync_org_catalog(session_factory=sf, org=org, settings=settings)
    return BifrostEnvelope(
        data=CatalogSyncResponse(
            org=result.org,
            total_fetched=result.total_fetched,
            manifest_hits=result.manifest_hits,
            errors=result.errors,
            status="complete",
        ),
    )
