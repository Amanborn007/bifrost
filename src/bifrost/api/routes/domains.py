"""Domain CRUD route handlers per TRD §4.2."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from bifrost.api.deps import AdminOnly, Repo, Settings
from bifrost.api.schemas.domains import (
    AddRepoDomainData,
    AddRepoDomainRequest,
    CreateDomainData,
    CreateDomainRequest,
    DeleteDomainData,
    DiscoverTeamReposData,
    DiscoverTeamReposRequest,
    DomainDetail,
    DomainRepoEntry,
    DomainSummary,
    RemoveRepoDomainData,
    TeamRepoEntry,
    UpdateDomainData,
    UpdateDomainRequest,
)
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.core.local_roots import (
    LocalPathNotAllowedError,
    ensure_local_path_allowed,
    local_path_from_url,
)
from bifrost.manifest.team_repos import discover_team_repos

router = APIRouter(prefix="/domains", tags=["domains"])


@router.post(
    "",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[CreateDomainData],
    status_code=status.HTTP_201_CREATED,
)
async def create_domain(body: CreateDomainRequest, repo: Repo) -> BifrostEnvelope[CreateDomainData]:
    """Create a new domain grouping."""
    domain_id = uuid.uuid4()
    urn = f"urn:bifrost:domain:{body.name}"
    domain = await repo.upsert_domain(
        id=domain_id,
        urn=urn,
        name=body.name,
        description=body.description,
    )
    return BifrostEnvelope(
        data=CreateDomainData(id=str(domain.id), urn=domain.urn),
    )


@router.get("", response_model=BifrostEnvelope[list[DomainSummary]])
async def list_domains(repo: Repo) -> BifrostEnvelope[list[DomainSummary]]:
    """List all domains."""
    domains = await repo.list_domains()
    summaries = []
    for d in domains:
        # Count repos via list_repos filtering — acceptable for V1 scale
        all_repos = await repo.list_repos()
        count = sum(1 for r in all_repos if r.domain_id == d.id)
        summaries.append(DomainSummary(id=str(d.id), name=d.name, repo_count=count))
    return BifrostEnvelope(data=summaries)


@router.get("/{domain_id}", response_model=BifrostEnvelope[DomainDetail])
async def get_domain(domain_id: str, repo: Repo) -> BifrostEnvelope[DomainDetail]:
    """Domain detail with repo list."""
    domain = await repo.get_domain(uuid.UUID(domain_id))
    if domain is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Domain not found")

    all_repos = await repo.list_repos()
    domain_repos = [r for r in all_repos if r.domain_id == domain.id]
    repo_entries: list[DomainRepoEntry] = []
    fresh_count = 0
    stale_count = 0
    total_entities = 0

    for r in domain_repos:
        freshness = await repo.get_repo_freshness(r.id)
        state = freshness.freshness_state if freshness else "unknown"
        last_indexed = (
            freshness.last_indexed_at.isoformat()
            if freshness and freshness.last_indexed_at
            else None
        )
        counts = await repo.count_entities_by_type_for_repo(r.id)
        latest = await repo.get_latest_extraction_task_for_repo(r.id)

        if state == "fresh":
            fresh_count += 1
        elif state in ("stale", "very_stale"):
            stale_count += 1
        total_entities += sum(counts.values())

        repo_entries.append(
            DomainRepoEntry(
                id=str(r.id),
                name=r.name,
                url=r.url,
                freshness_state=state,
                last_indexed_at=last_indexed,
                entity_counts=counts,
                latest_task_id=str(latest.id) if latest else None,
                latest_task_status=latest.status if latest else None,
            )
        )

    # Sort by freshness state (fresh → stale → unknown), then name
    state_order = {"fresh": 0, "stale": 1, "very_stale": 2, "unknown": 3}
    repo_entries.sort(key=lambda e: (state_order.get(e.freshness_state, 4), e.name))

    return BifrostEnvelope(
        data=DomainDetail(
            id=str(domain.id),
            name=domain.name,
            description=domain.description,
            repos=repo_entries,
            cross_repo_flow_count=0,  # V1: not yet computed
            total_entities=total_entities,
            fresh_count=fresh_count,
            stale_count=stale_count,
        ),
    )


@router.put(
    "/{domain_id}", dependencies=[AdminOnly], response_model=BifrostEnvelope[UpdateDomainData]
)
async def update_domain(
    domain_id: str, body: UpdateDomainRequest, repo: Repo
) -> BifrostEnvelope[UpdateDomainData]:
    """Update domain name or description."""
    domain = await repo.get_domain(uuid.UUID(domain_id))
    if domain is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Domain not found")

    updates: dict[str, object] = {"id": domain.id, "urn": domain.urn}
    if body.name is not None:
        updates["name"] = body.name
    else:
        updates["name"] = domain.name
    if body.description is not None:
        updates["description"] = body.description

    await repo.upsert_domain(**updates)
    return BifrostEnvelope(data=UpdateDomainData(id=domain_id))


@router.delete(
    "/{domain_id}", dependencies=[AdminOnly], response_model=BifrostEnvelope[DeleteDomainData]
)
async def delete_domain(domain_id: str, repo: Repo) -> BifrostEnvelope[DeleteDomainData]:
    """Delete a domain. Does NOT delete repos — only removes the grouping."""
    domain = await repo.get_domain(uuid.UUID(domain_id))
    if domain is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Domain not found")
    # V1: return status — actual deletion is future work
    return BifrostEnvelope(data=DeleteDomainData(id=domain_id))


@router.post(
    "/{domain_id}/repos",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[AddRepoDomainData],
    status_code=status.HTTP_201_CREATED,
)
async def add_repo_to_domain(
    domain_id: str, body: AddRepoDomainRequest, repo: Repo
) -> BifrostEnvelope[AddRepoDomainData]:
    """Add a repo to a domain."""
    domain = await repo.get_domain(uuid.UUID(domain_id))
    if domain is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Domain not found")

    db_repo = await repo.get_repo(uuid.UUID(body.repo_id))
    if db_repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found")

    # Update the repo's domain_id
    await repo.upsert_repo(
        id=db_repo.id,
        urn=db_repo.urn,
        name=db_repo.name,
        url=db_repo.url,
        domain_id=domain.id,
        adapter_config=db_repo.adapter_config,
    )

    return BifrostEnvelope(
        data=AddRepoDomainData(domain_id=domain_id, repo_id=body.repo_id),
    )


@router.post(
    "/{domain_id}/discover-repos",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[DiscoverTeamReposData],
)
async def discover_domain_repos(
    domain_id: str,
    body: DiscoverTeamReposRequest,
    repo: Repo,
    settings: Settings,
) -> BifrostEnvelope[DiscoverTeamReposData]:
    """Discover and link every repo whose manifest names ``team``.

    - Searches the configured GitHub orgs through the authenticated ``gh`` CLI.
    - Verifies each candidate by loading its manifest through the enabled
      providers, so a search hit that merely mentions the team is filtered out.
    - Unregistered repos are created and linked. Already-registered repos get
      their domain_id set if missing. Pre-linked repos are reported as-is.
    - ``dry_run: true`` skips all DB writes and returns what would change.
    - ``trigger_extraction: true`` queues an initial extraction for each
      newly-added repo (ignored when dry_run).
    """
    domain = await repo.get_domain(uuid.UUID(domain_id))
    if domain is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Domain not found")

    team = (body.team or domain.name).strip()
    matches = await discover_team_repos(team, settings=settings)

    existing_repos = await repo.list_repos()
    by_url = {r.url: r for r in existing_repos}

    added: list[TeamRepoEntry] = []
    linked: list[TeamRepoEntry] = []
    already_linked: list[TeamRepoEntry] = []
    errors: list[TeamRepoEntry] = []
    to_submit: list[str] = []

    for m in matches:
        existing = by_url.get(m.git_url)
        if existing is None:
            local_path = local_path_from_url(m.git_url)
            if local_path is not None:
                try:
                    ensure_local_path_allowed(local_path, settings)
                except LocalPathNotAllowedError as exc:
                    errors.append(
                        TeamRepoEntry(
                            full_name=m.full_name,
                            action="error",
                            git_url=m.git_url,
                            description=m.description,
                            error=str(exc),
                        )
                    )
                    continue
            if body.dry_run:
                added.append(
                    TeamRepoEntry(
                        full_name=m.full_name,
                        action="added",
                        git_url=m.git_url,
                        description=m.description,
                    )
                )
                continue
            created = await repo.upsert_repo(
                id=uuid.uuid4(),
                urn=f"urn:bifrost:repo:{m.full_name.replace('/', ':')}",
                name=m.name,
                url=m.git_url,
                domain_id=domain.id,
                adapter_config={"description": m.description or "", "team": m.team},
            )
            entry = TeamRepoEntry(
                full_name=m.full_name,
                action="added",
                repo_id=str(created.id),
                git_url=m.git_url,
                description=m.description,
            )
            if body.trigger_extraction:
                task = await repo.create_task(
                    task_type="extraction",
                    query=f"Extract knowledge from {m.name}",
                    scope_json={"repo_ids": [str(created.id)], "force_full": True},
                    budget_json={},
                )
                to_submit.append(str(task.id))
                entry.task_id = str(task.id)
            added.append(entry)
        elif existing.domain_id is None:
            if not body.dry_run:
                await repo.upsert_repo(
                    id=existing.id,
                    urn=existing.urn,
                    name=existing.name,
                    url=existing.url,
                    domain_id=domain.id,
                    adapter_config=existing.adapter_config,
                )
            linked.append(
                TeamRepoEntry(
                    full_name=m.full_name,
                    action="linked",
                    repo_id=str(existing.id),
                    git_url=m.git_url,
                    description=m.description,
                )
            )
        elif existing.domain_id == domain.id:
            already_linked.append(
                TeamRepoEntry(
                    full_name=m.full_name,
                    action="already_linked",
                    repo_id=str(existing.id),
                    git_url=m.git_url,
                    description=m.description,
                )
            )
        else:
            errors.append(
                TeamRepoEntry(
                    full_name=m.full_name,
                    action="error",
                    repo_id=str(existing.id),
                    git_url=m.git_url,
                    description=m.description,
                    error=f"already linked to a different domain ({existing.domain_id})",
                )
            )

    if to_submit:
        await repo.commit()
        from bifrost.extraction.worker import get_worker

        worker = get_worker()
        for task_id in to_submit:
            worker.submit(task_id)

    return BifrostEnvelope(
        data=DiscoverTeamReposData(
            team=team,
            dry_run=body.dry_run,
            candidates_found=len(matches),
            providers=list(settings.manifest.providers),
            added=added,
            linked=linked,
            already_linked=already_linked,
            errors=errors,
        ),
    )


@router.delete(
    "/{domain_id}/repos/{repo_id}",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[RemoveRepoDomainData],
)
async def remove_repo_from_domain(
    domain_id: str, repo_id: str, repo: Repo
) -> BifrostEnvelope[RemoveRepoDomainData]:
    """Remove a repo from a domain."""
    db_repo = await repo.get_repo(uuid.UUID(repo_id))
    if db_repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found")

    # Clear domain_id
    await repo.upsert_repo(
        id=db_repo.id,
        urn=db_repo.urn,
        name=db_repo.name,
        url=db_repo.url,
        domain_id=None,
        adapter_config=db_repo.adapter_config,
    )

    return BifrostEnvelope(
        data=RemoveRepoDomainData(domain_id=domain_id, repo_id=repo_id),
    )
