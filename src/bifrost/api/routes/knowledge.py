"""Knowledge tab endpoint — everything the per-repo control panel needs
in a single request so the page doesn't fan out to 3 endpoints per row.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bifrost.api.deps import DB
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.storage.models import (
    Conversation,
    Entity,
    OrgRepo,
    Repo,
    RepoOwnership,
)
from bifrost.storage.models import (
    ExtractionTask as Task,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class KnowledgeRepo(BaseModel):
    """One row in the Knowledge tab's repo table."""

    id: str
    name: str
    url: str
    domain_id: str | None
    domain_name: str | None

    # Freshness
    freshness_state: str  # fresh | stale | very_stale | unknown
    index_status: str  # complete | in_progress | not_started
    last_indexed_at: str | None
    last_commit_indexed: str | None  # sha
    component_count: int
    flow_count: int
    dependency_count: int

    # Ownership — manifest derived (squad) + discovered (jira)
    squad: str | None
    service_tier: str | None
    # Geographic region from ``OrgRepo.region`` — looked up
    # via the registered_repo_id back-reference. NULL when the repo
    # isn't in the catalog or its manifest didn't surface a region.
    region: str | None
    jira_project_key: str | None
    confluence_space_key: str | None
    slack_channel: str | None
    compliance_tags: list[str]
    downstream_services_count: int

    # Latest extraction task (so UI can surface "running" banners)
    latest_task_id: str | None
    latest_task_status: str | None
    latest_task_updated_at: str | None

    # AI-generated description from Phase J — None until first full extraction
    summary: str | None = None
    # Chat queries scoped to this repo in the last 7 days (re-index priority)
    queries_last_7d: int = 0
    # Names of downstream callers from the manifest dependency graph
    downstream_service_names: list[str] = []


class KnowledgeOverview(BaseModel):
    total: int
    fresh: int
    stale: int
    not_indexed: int
    by_domain: dict[str, int]
    repos: list[KnowledgeRepo]


async def _latest_task_for_repo(
    session: AsyncSession,
    repo_id: UUID,
) -> Task | None:
    """Most recent extraction task that targets this repo."""
    stmt = (
        select(Task)
        .where(Task.task_type == "extraction")
        .where(Task.scope_json["repo_ids"].astext.contains(str(repo_id)))
        .order_by(Task.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


@router.get("/repos", response_model=BifrostEnvelope[KnowledgeOverview])
async def knowledge_repos(session: DB) -> BifrostEnvelope[KnowledgeOverview]:
    """Return every registered repo with freshness + ownership + latest task.

    Used by the Knowledge tab — one round-trip, no waterfalls.
    """
    # Eager-load everything we need: freshness, ownership, domain.
    stmt = select(Repo).options(
        selectinload(Repo.freshness),
        selectinload(Repo.domain),
    )
    repos = (await session.execute(stmt)).scalars().all()

    # Pull ownership rows in one go
    own_stmt = select(RepoOwnership).where(
        RepoOwnership.repo_id.in_([r.id for r in repos]),
    )
    own_rows = (await session.execute(own_stmt)).scalars().all()
    own_by_id = {o.repo_id: o for o in own_rows}

    # Pull org_repos (catalog) rows for the region lookup. Region
    # lives on OrgRepo (parsed from the manifest at catalog-sync time);
    # we join via ``registered_repo_id`` since multiple OrgRepos can
    # in principle map to the same Repo. Most repos have at most one
    # mapping; if there are multiple, we take whichever the SELECT
    # returns last — they should all carry the same region.
    org_stmt = select(OrgRepo).where(
        OrgRepo.registered_repo_id.in_([r.id for r in repos]),
    )
    org_rows = (await session.execute(org_stmt)).scalars().all()
    org_region_by_id: dict[UUID, str | None] = {}
    for o in org_rows:
        if o.registered_repo_id is None:
            continue
        org_region_by_id[o.registered_repo_id] = o.region

    # Entity counts grouped by (repo_id, entity_type) in one query
    from sqlalchemy import func

    ec_stmt = (
        select(
            Entity.scope_repo_id,
            Entity.entity_type,
            func.count(Entity.id),
        )
        .where(Entity.scope_repo_id.isnot(None))
        .group_by(Entity.scope_repo_id, Entity.entity_type)
    )
    counts: dict[UUID, dict[str, int]] = {}
    for rid, etype, cnt in (await session.execute(ec_stmt)).all():
        counts.setdefault(rid, {})[etype] = int(cnt)

    # Conversations touched in the last 7 days, grouped by each repo in their scope.
    scoped_repo_id = func.jsonb_array_elements_text(Conversation.scope_repo_ids).column_valued(
        "scoped_repo_id"
    )
    conv_stmt = (
        select(scoped_repo_id, func.count(Conversation.id))
        .where(Conversation.deleted_at.is_(None))
        .where(Conversation.updated_at >= func.now() - text("interval '7 days'"))
        .group_by(scoped_repo_id)
    )
    conv_counts: dict[str, int] = {
        str(rid): int(cnt) for rid, cnt in (await session.execute(conv_stmt)).all()
    }

    # Latest task per repo — one query per repo for now (10s of repos; fine).
    tasks_by_repo: dict[UUID, Task | None] = {}
    for r in repos:
        tasks_by_repo[r.id] = await _latest_task_for_repo(session, r.id)

    out: list[KnowledgeRepo] = []
    fresh = stale = not_indexed = 0
    by_domain: dict[str, int] = {}

    for r in repos:
        f = r.freshness
        state = f.freshness_state if f else "unknown"
        if state == "fresh":
            fresh += 1
        elif state in ("stale", "very_stale", "slightly_stale"):
            stale += 1
        else:
            not_indexed += 1

        d_name = r.domain.name if r.domain else "(no domain)"
        by_domain[d_name] = by_domain.get(d_name, 0) + 1

        entity_c = counts.get(r.id, {})
        own = own_by_id.get(r.id)
        task = tasks_by_repo.get(r.id)

        out.append(
            KnowledgeRepo(
                id=str(r.id),
                name=r.name,
                url=r.url,
                domain_id=str(r.domain_id) if r.domain_id else None,
                domain_name=r.domain.name if r.domain else None,
                freshness_state=state,
                index_status=f.index_status if f else "not_started",
                last_indexed_at=f.last_indexed_at.isoformat() if f and f.last_indexed_at else None,
                last_commit_indexed=f.last_commit_indexed if f else None,
                component_count=entity_c.get("component", 0),
                flow_count=entity_c.get("flow", 0),
                dependency_count=entity_c.get("dependency", 0),
                squad=own.squad if own else None,
                service_tier=own.service_tier if own else None,
                region=org_region_by_id.get(r.id),
                jira_project_key=own.jira_project_key if own else None,
                confluence_space_key=own.confluence_space_key if own else None,
                slack_channel=own.slack_channel if own else None,
                compliance_tags=list(own.compliance_tags) if own else [],
                downstream_services_count=len(own.downstream_services) if own else 0,
                latest_task_id=str(task.id) if task else None,
                latest_task_status=task.status if task else None,
                latest_task_updated_at=task.updated_at.isoformat() if task else None,
                summary=r.description or None,
                queries_last_7d=conv_counts.get(str(r.id), 0),
                downstream_service_names=sorted(own.downstream_services.keys())
                if own and own.downstream_services
                else [],
            )
        )

    out.sort(key=lambda x: (x.domain_name or "zzz", x.name))

    return BifrostEnvelope(
        data=KnowledgeOverview(
            total=len(out),
            fresh=fresh,
            stale=stale,
            not_indexed=not_indexed,
            by_domain=by_domain,
            repos=out,
        ),
    )


# ── Cross-repo dependency graph ──────────────────────────────────────


class CrossRepoDep(BaseModel):
    repo_id: str
    repo_name: str
    edge_count: int
    rel_types: list[str]


class CrossRepoDepsResponse(BaseModel):
    """Which repos this one's entities reference, and which ones reference it."""

    calls: list[CrossRepoDep]  # repos this service's entities link out to
    called_by: list[CrossRepoDep]  # repos whose entities link in to this one
    total_cross_repo_edges: int


@router.get(
    "/repos/{repo_id}/cross-repo-deps",
    response_model=BifrostEnvelope[CrossRepoDepsResponse],
)
async def cross_repo_deps(repo_id: str, session: DB) -> BifrostEnvelope[CrossRepoDepsResponse]:
    """Return cross-repo relationship edges for a registered repo.

    Requires Phase G to have run on the repo.  Returns empty lists
    until the first full re-index after the Phase G persistence fix.
    """
    from uuid import UUID

    from sqlalchemy import text as sql_text

    try:
        rid = UUID(repo_id)
    except ValueError:
        return BifrostEnvelope(
            data=CrossRepoDepsResponse(calls=[], called_by=[], total_cross_repo_edges=0)
        )

    _params = {"repo_id": rid}

    # Repos this service's entities link OUT to
    calls_q = sql_text("""
        SELECT
            r.id::text            AS repo_id,
            r.name                AS repo_name,
            COUNT(er.id)          AS edge_count,
            ARRAY_AGG(DISTINCT er.rel_type)::text[]  AS rel_types
        FROM entity_relationships er
        JOIN entities se ON er.source_id = se.id
        JOIN entities te ON er.target_id = te.id
        JOIN repos    r  ON te.scope_repo_id = r.id
        WHERE se.scope_repo_id = :repo_id
          AND te.scope_repo_id != :repo_id
          AND te.scope_repo_id IS NOT NULL
        GROUP BY r.id, r.name
        ORDER BY edge_count DESC
        LIMIT 25
    """)

    # Repos whose entities link IN to this service
    called_by_q = sql_text("""
        SELECT
            r.id::text            AS repo_id,
            r.name                AS repo_name,
            COUNT(er.id)          AS edge_count,
            ARRAY_AGG(DISTINCT er.rel_type)::text[]  AS rel_types
        FROM entity_relationships er
        JOIN entities se ON er.source_id = se.id
        JOIN entities te ON er.target_id = te.id
        JOIN repos    r  ON se.scope_repo_id = r.id
        WHERE te.scope_repo_id = :repo_id
          AND se.scope_repo_id != :repo_id
          AND se.scope_repo_id IS NOT NULL
        GROUP BY r.id, r.name
        ORDER BY edge_count DESC
        LIMIT 25
    """)

    calls_rows = (await session.execute(calls_q, _params)).all()
    called_by_rows = (await session.execute(called_by_q, _params)).all()

    calls = [
        CrossRepoDep(
            repo_id=row.repo_id,
            repo_name=row.repo_name,
            edge_count=int(row.edge_count),
            rel_types=list(row.rel_types or []),
        )
        for row in calls_rows
    ]
    called_by = [
        CrossRepoDep(
            repo_id=row.repo_id,
            repo_name=row.repo_name,
            edge_count=int(row.edge_count),
            rel_types=list(row.rel_types or []),
        )
        for row in called_by_rows
    ]

    return BifrostEnvelope(
        data=CrossRepoDepsResponse(
            calls=calls,
            called_by=called_by,
            total_cross_repo_edges=sum(d.edge_count for d in calls)
            + sum(d.edge_count for d in called_by),
        )
    )
