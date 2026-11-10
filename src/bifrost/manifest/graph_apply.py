"""Persist a ``GraphPlan`` and build one from the registered repos and catalog.

Upserts are single-statement ``INSERT ... ON CONFLICT`` with rows in a
stable order, so concurrent applies serialize on the unique indexes
instead of colliding.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.manifest.files import GhError, RepoFileError
from bifrost.manifest.graph_plan import GraphPlan, PlannedEdge, plan_service_graph
from bifrost.manifest.loader import load_manifest
from bifrost.manifest.model import ServiceManifest
from bifrost.research.confidence_policy import build_source_basis, classify
from bifrost.research.ownership_sync import resolve_repo_files
from bifrost.storage.models import Domain, Entity, EntityRelationship, OrgRepo, Repo
from bifrost.storage.sql_util import affected_rows

logger = logging.getLogger(__name__)

_CHUNK = 500
_SERVICE_PREFIX = "urn:bifrost:service:"


@dataclass
class ApplyResult:
    services: int = 0
    domains: int = 0
    edges: int = 0
    edges_created: int = 0
    repos_linked: int = 0


async def _upsert_domains(session: AsyncSession, plan: GraphPlan) -> dict[str, uuid.UUID]:
    """Domain urn -> id. Existing domains match by name first, then by urn."""
    if not plan.domains:
        return {}
    by_name = {
        d.name.lower(): d.id for d in (await session.execute(select(Domain))).scalars().all()
    }
    ids: dict[str, uuid.UUID] = {}
    for planned in sorted(plan.domains, key=lambda d: d.urn):
        existing = by_name.get(planned.name.lower())
        if existing is not None:
            ids[planned.urn] = existing
            continue
        stmt = (
            pg_insert(Domain)
            .values(
                id=uuid.uuid4(),
                urn=planned.urn,
                name=planned.name,
                description=f"Domain derived from the service manifest team {planned.name}",
            )
            .on_conflict_do_update(index_elements=["urn"], set_={"urn": planned.urn})
            .returning(Domain.id)
        )
        ids[planned.urn] = (await session.execute(stmt)).scalar_one()
    return ids


async def _upsert_services(
    session: AsyncSession,
    plan: GraphPlan,
    domain_ids: dict[str, uuid.UUID],
) -> dict[str, uuid.UUID]:
    """Service urn -> entity id."""
    if not plan.services:
        return {}
    repo_rows = (await session.execute(select(Repo.id, Repo.name, Repo.url))).all()
    repo_by_name = {r.name: r for r in repo_rows}
    level, confidence = classify("manifest")

    rows: list[dict[str, Any]] = []
    for svc in sorted(plan.services, key=lambda s: s.urn):
        repo = repo_by_name.get(svc.repo)
        meta = dict(svc.metadata)
        if repo is not None:
            meta["repo_url"] = repo.url
        rows.append(
            {
                "id": uuid.uuid4(),
                "entity_type": "service",
                "urn": svc.urn,
                "name": svc.name,
                "qualified_name": svc.repo,
                "scope_repo_id": repo.id if repo is not None else None,
                "scope_domain_id": domain_ids.get(svc.domain_urn) if svc.domain_urn else None,
                "entity_metadata": meta,
                "confidence": confidence,
                "confidence_level": level,
                "freshness_state": "fresh",
                "last_refreshed": datetime.now(),
                "source_basis": build_source_basis(
                    "manifest",
                    reference=f"manifest:{svc.repo}",
                    method="declared",
                ),
            }
        )

    ids: dict[str, uuid.UUID] = {}
    for i in range(0, len(rows), _CHUNK):
        stmt = pg_insert(Entity).values(rows[i : i + _CHUNK])
        update_cols = {
            col: stmt.excluded[col]
            for col in (
                "entity_type",
                "name",
                "qualified_name",
                "scope_repo_id",
                "scope_domain_id",
                "metadata",
                "confidence",
                "confidence_level",
                "freshness_state",
                "last_refreshed",
                "source_basis",
            )
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["urn"], set_={**update_cols, "updated_at": func.now()}
        ).returning(Entity.urn, Entity.id)
        for urn, entity_id in (await session.execute(stmt)).all():
            ids[urn] = entity_id
    return ids


def _edge_row(edge: PlannedEdge, ids: dict[str, uuid.UUID]) -> dict[str, Any] | None:
    source_id = ids.get(edge.source_urn)
    target_id = ids.get(edge.target_urn)
    if source_id is None or target_id is None:
        return None
    return {
        "id": uuid.uuid4(),
        "source_id": source_id,
        "target_id": target_id,
        "rel_type": edge.rel_type,
        "confidence": edge.confidence,
        "freshness_state": "fresh",
        "source_basis": build_source_basis(
            "manifest",
            reference=f"manifest:{edge.source_urn.removeprefix(_SERVICE_PREFIX)}",
            method=edge.method,
            matched_on=edge.matched_on,
        ),
    }


async def _insert_edges(session: AsyncSession, plan: GraphPlan, ids: dict[str, uuid.UUID]) -> int:
    rows = [
        row
        for edge in sorted(plan.edges, key=lambda e: (e.source_urn, e.target_urn, e.rel_type))
        if (row := _edge_row(edge, ids)) is not None
    ]
    created = 0
    for i in range(0, len(rows), _CHUNK):
        stmt = (
            pg_insert(EntityRelationship)
            .values(rows[i : i + _CHUNK])
            .on_conflict_do_nothing(index_elements=["source_id", "target_id", "rel_type"])
        )
        created += affected_rows(await session.execute(stmt))
    return created


async def _link_repo_domains(
    session: AsyncSession, plan: GraphPlan, domain_ids: dict[str, uuid.UUID]
) -> int:
    linked = 0
    for svc in sorted(plan.services, key=lambda s: s.urn):
        domain_id = domain_ids.get(svc.domain_urn) if svc.domain_urn else None
        if domain_id is None:
            continue
        result = await session.execute(
            update(Repo)
            .where(Repo.name == svc.repo, Repo.domain_id.is_(None))
            .values(domain_id=domain_id)
        )
        linked += affected_rows(result)
    return linked


async def apply_graph_plan(
    session_factory: async_sessionmaker[AsyncSession], plan: GraphPlan
) -> ApplyResult:
    """Upsert the planned domains and services, insert missing edges, link repos to domains."""
    async with session_factory() as session:
        domain_ids = await _upsert_domains(session, plan)
        ids = await _upsert_services(session, plan, domain_ids)
        created = await _insert_edges(session, plan, ids)
        linked = await _link_repo_domains(session, plan, domain_ids)
        await session.commit()
    return ApplyResult(
        services=len(ids),
        domains=len(domain_ids),
        edges=len(plan.edges),
        edges_created=created,
        repos_linked=linked,
    )


async def collect_manifests(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    include_catalog: bool,
    settings: BifrostSettings,
    concurrency: int = 8,
) -> tuple[dict[str, ServiceManifest], dict[str, str]]:
    """Manifests by repo name, plus per-repo load errors.

    Registered repos always get a node. Catalog repos only get one when a
    provider actually found a manifest.
    """
    async with session_factory() as session:
        registered = (await session.execute(select(Repo))).scalars().all()
        catalog = (
            (
                await session.execute(
                    select(OrgRepo)
                    .where(OrgRepo.archived.is_(False), OrgRepo.is_fork.is_(False))
                    .order_by(OrgRepo.owner, OrgRepo.name)
                )
            )
            .scalars()
            .all()
            if include_catalog
            else []
        )

    targets: dict[str, tuple[str, str | None, bool]] = {}
    for repo in registered:
        targets[repo.name] = (repo.url, repo.local_clone_path, True)
    for row in catalog:
        targets.setdefault(row.name, (row.url, None, False))

    sem = asyncio.Semaphore(concurrency)
    manifests: dict[str, ServiceManifest] = {}
    errors: dict[str, str] = {}

    async def _one(name: str, url: str, clone: str | None, is_registered: bool) -> None:
        async with sem:
            files = resolve_repo_files(url, clone, settings)
            if files is None:
                errors[name] = f"no readable source for {url}"
                return
            try:
                md = await load_manifest(
                    files, repo_name=name, cfg=settings.manifest, conventions=settings.conventions
                )
            except (GhError, RepoFileError, OSError) as exc:
                errors[name] = str(exc)[:200]
                return
            if is_registered or md.providers:
                manifests[name] = md

    await asyncio.gather(*(_one(n, u, c, r) for n, (u, c, r) in targets.items()))
    for name, message in sorted(errors.items()):
        logger.warning("service graph: skipped %s: %s", name, message)
    return manifests, errors


async def build_service_graph(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    include_catalog: bool,
    settings: BifrostSettings | None = None,
    errors: dict[str, str] | None = None,
) -> GraphPlan:
    """Plan the service graph for registered repos (and catalog repos with a manifest).

    Repos whose files could not be read are logged and left out; pass
    ``errors`` to receive them by repo name.
    """
    settings = settings or get_settings()
    manifests, failed = await collect_manifests(
        session_factory, include_catalog=include_catalog, settings=settings
    )
    if errors is not None:
        errors.update(failed)
    return plan_service_graph(manifests)


class GraphRefresher:
    """Coalesces rebuild requests: N concurrent requests cost at most one extra rebuild."""

    def __init__(self, rebuild: Callable[[], Awaitable[object]]) -> None:
        self._rebuild = rebuild
        self._lock = asyncio.Lock()
        self._dirty = False

    async def request(self) -> None:
        self._dirty = True
        async with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            await self._rebuild()

    @classmethod
    def for_settings(
        cls,
        session_factory: async_sessionmaker[AsyncSession],
        settings: BifrostSettings,
        *,
        include_catalog: bool = False,
    ) -> GraphRefresher:
        async def rebuild() -> ApplyResult:
            plan = await build_service_graph(
                session_factory, include_catalog=include_catalog, settings=settings
            )
            return await apply_graph_plan(session_factory, plan)

        return cls(rebuild)


_refresher: tuple[tuple[int, int], GraphRefresher] | None = None
_background: set[asyncio.Task[None]] = set()


def get_graph_refresher(
    session_factory: async_sessionmaker[AsyncSession], settings: BifrostSettings
) -> GraphRefresher:
    """Process-wide refresher, so every caller shares one lock."""
    global _refresher  # noqa: PLW0603
    key = (id(session_factory), id(settings))
    if _refresher is None or _refresher[0] != key:
        _refresher = (key, GraphRefresher.for_settings(session_factory, settings))
    return _refresher[1]


def spawn_background(coro: Awaitable[None], *, what: str) -> asyncio.Task[None]:
    """Run ``coro`` fire-and-forget, keeping a reference and logging any exception."""

    async def _run() -> None:
        try:
            await coro
        except Exception:
            logger.exception("%s failed", what)

    task = asyncio.create_task(_run())
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task
