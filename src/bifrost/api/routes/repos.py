"""Repo CRUD route handlers per TRD §4.1."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from bifrost.api.deps import AdminOnly, Repo, get_session_factory
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.api.schemas.repos import (
    DeregisterRepoData,
    RegisterRepoData,
    RegisterRepoRequest,
    RepoCheckpointPhase,
    RepoCheckpointStatus,
    RepoDetail,
    RepoFreshness,
    RepoStats,
    RepoSummary,
    TriggerExtractionData,
    TriggerExtractionRequest,
)
from bifrost.cli.config import get_settings
from bifrost.core.crypto import RESERVED_PREFIX, misplaced_secret_keys, redact_secrets
from bifrost.core.local_roots import (
    LocalPathNotAllowedError,
    ensure_local_path_allowed,
    local_path_from_url,
)
from bifrost.extraction.checkpoint import PHASE_ORDER, ExtractionCheckpoint
from bifrost.manifest.graph_apply import get_graph_refresher, spawn_background
from bifrost.research.ownership_sync import sync_repo_ownership

_PHASE_LABELS: dict[str, str] = {
    "A": "Triage",
    "B": "Reserved",
    "C": "Reserved",
    "D": "Components",
    "E": "Flows",
    "F": "Dependencies",
    "G": "Relations",
    "H": "Knowledge index",
    "I": "Confidence",
}

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/repos", tags=["repos"])


@router.post(
    "",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[RegisterRepoData],
    status_code=status.HTTP_201_CREATED,
)
async def register_repo(
    body: RegisterRepoRequest,
    repo: Repo,
) -> BifrostEnvelope[RegisterRepoData]:
    """Register a new repository."""
    adapter_config = body.adapter_config or {}
    misplaced = misplaced_secret_keys(adapter_config)
    if misplaced:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"adapter_config keys {misplaced} look like credentials. Credentials are "
                f"only stored encrypted under the {RESERVED_PREFIX!r} namespace, "
                f"e.g. {RESERVED_PREFIX}token."
            ),
        )
    repo_id = uuid.uuid4()
    urn = f"urn:bifrost:repo:{body.name}"

    # Check for duplicate URL
    existing = await repo.get_repo_by_urn(urn)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Repository with URN {urn} already registered",
        )

    settings = get_settings()
    local_path = local_path_from_url(body.url)
    if local_path is not None:
        try:
            ensure_local_path_allowed(local_path, settings)
        except LocalPathNotAllowedError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    domain_id = uuid.UUID(body.domain_id) if body.domain_id else None
    db_repo = await repo.upsert_repo(
        id=repo_id,
        urn=urn,
        name=body.name,
        url=body.url,
        domain_id=domain_id,
        description=body.description,
        adapter_config=adapter_config,
    )

    # The background sync opens its own session, so the row must be durable first.
    await repo.commit()

    session_factory = get_session_factory(settings)
    refresher = get_graph_refresher(session_factory, settings)
    repo_id_str = str(db_repo.id)

    async def _sync_then_refresh() -> None:
        result = await sync_repo_ownership(
            session_factory=session_factory, repo_id=repo_id_str, settings=settings
        )
        if result.error:
            logger.warning("ownership sync for %s: %s", body.name, result.error)
        await refresher.request()

    spawn_background(_sync_then_refresh(), what=f"ownership sync for {body.name}")

    return BifrostEnvelope(
        data=RegisterRepoData(
            id=str(db_repo.id),
            urn=db_repo.urn,
            adapter_config=redact_secrets(db_repo.adapter_config),
        ),
    )


@router.post(
    "/{repo_id}/sync-ownership",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[dict],
)
async def sync_ownership(repo_id: str, repo: Repo) -> BifrostEnvelope[dict]:
    """Re-read the repo's manifest and refresh its ownership metadata.

    Idempotent. Use this when the manifest has been edited (new team,
    new *_HOST entries, etc.) and you want the research agents to pick
    up the change without restarting.
    """
    existing = await repo.get_repo(uuid.UUID(repo_id))
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repo not found",
        )

    settings = get_settings()
    session_factory = get_session_factory(settings)
    result = await sync_repo_ownership(
        session_factory=session_factory, repo_id=repo_id, settings=settings
    )
    if result.status != "error":
        spawn_background(
            get_graph_refresher(session_factory, settings).request(),
            what=f"service graph refresh after syncing {existing.name}",
        )
    return BifrostEnvelope(
        data={
            "status": result.status,
            "squad": result.squad,
            "downstream_services_count": result.downstream_count,
            "error": result.error,
        },
    )


@router.get("", response_model=BifrostEnvelope[list[RepoSummary]])
async def list_repos(repo: Repo) -> BifrostEnvelope[list[RepoSummary]]:
    """List all registered repos."""
    repos = await repo.list_repos()
    summaries = []
    for r in repos:
        freshness = await repo.get_repo_freshness(r.id)
        summaries.append(
            RepoSummary(
                id=str(r.id),
                urn=r.urn,
                name=r.name,
                url=r.url,
                domain_id=str(r.domain_id) if r.domain_id else None,
                freshness_state=freshness.freshness_state if freshness else "unknown",
                index_status=freshness.index_status if freshness else "not_started",
                last_indexed_at=freshness.last_indexed_at if freshness else None,
            )
        )
    return BifrostEnvelope(data=summaries)


@router.get("/{repo_id}", response_model=BifrostEnvelope[RepoDetail])
async def get_repo(repo_id: str, repo: Repo) -> BifrostEnvelope[RepoDetail]:
    """Repo detail with freshness and stats."""
    db_repo = await repo.get_repo(uuid.UUID(repo_id))
    if db_repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found")

    freshness = await repo.get_repo_freshness(db_repo.id)
    return BifrostEnvelope(
        data=RepoDetail(
            id=str(db_repo.id),
            urn=db_repo.urn,
            name=db_repo.name,
            url=db_repo.url,
            domain_id=str(db_repo.domain_id) if db_repo.domain_id else None,
            freshness=RepoFreshness(
                state=freshness.freshness_state if freshness else "unknown",
                last_commit_indexed=freshness.last_commit_indexed if freshness else None,
                last_indexed_at=freshness.last_indexed_at if freshness else None,
                pending_commits=freshness.pending_commits if freshness else 0,
            ),
            stats=RepoStats(
                component_count=freshness.component_count if freshness else 0,
                flow_count=freshness.flow_count if freshness else 0,
                dependency_count=freshness.dependency_count if freshness else 0,
            ),
        ),
    )


@router.delete(
    "/{repo_id}", dependencies=[AdminOnly], response_model=BifrostEnvelope[DeregisterRepoData]
)
async def deregister_repo(repo_id: str, repo: Repo) -> BifrostEnvelope[DeregisterRepoData]:
    """Deregister (soft delete) a repository."""
    db_repo = await repo.get_repo(uuid.UUID(repo_id))
    if db_repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found")
    # For V1, just return the deregistered status — actual cleanup is future work
    return BifrostEnvelope(data=DeregisterRepoData(id=str(db_repo.id)))


@router.post(
    "/{repo_id}/extract",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[TriggerExtractionData],
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_extraction(
    repo_id: str,
    body: TriggerExtractionRequest,
    repo: Repo,
) -> BifrostEnvelope[TriggerExtractionData]:
    """Trigger extraction for a repository. Returns task ID for status tracking."""
    db_repo = await repo.get_repo(uuid.UUID(repo_id))
    if db_repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found")

    scope: dict = {"repo_ids": [str(db_repo.id)], "force_full": body.force_full}
    if body.max_files is not None:
        scope["max_files"] = int(body.max_files)
    if body.invalidate_phases:
        # Validate against canonical phase ordering (A, B, C, D, E, F, G, H, I).
        valid_phases = {"A", "B", "C", "D", "E", "F", "G", "H", "I"}
        bad = [p for p in body.invalidate_phases if p not in valid_phases]
        if bad:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid phase(s) to invalidate: {bad}. Must be in {sorted(valid_phases)}",
            )
        scope["invalidate_phases"] = list(body.invalidate_phases)

    task = await repo.create_task(
        task_type="extraction",
        query=f"Extract knowledge from {db_repo.name}",
        scope_json=scope,
        budget_json=body.budget_override or {},
    )

    # The request session commits only after the response, so commit first or
    # the worker's own session cannot see the task row.
    await repo.commit()

    from bifrost.extraction.worker import get_worker

    get_worker().submit(str(task.id))

    return BifrostEnvelope(data=TriggerExtractionData(task_id=str(task.id)))


@router.get(
    "/{repo_id}/checkpoint",
    response_model=BifrostEnvelope[RepoCheckpointStatus],
)
async def get_repo_checkpoint(repo_id: str, repo: Repo) -> BifrostEnvelope[RepoCheckpointStatus]:
    """Return the repo's extraction checkpoint — which phases are done.

    Powers the Resume UI: lets users see that, e.g., phases A-D completed
    on a past task and only E-I would run on a new Resume invocation.
    """
    db_repo = await repo.get_repo(uuid.UUID(repo_id))
    if db_repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found")

    settings = get_settings()
    cp = ExtractionCheckpoint(
        checkpoint_dir=settings.checkpoint_dir,
        session_id=f"repo-{repo_id}",
    )
    completed = cp.get_completed_phases()
    last_updated_raw = cp._state.get("last_updated")  # internal but cheap
    from datetime import datetime as _dt

    last_updated = (
        _dt.fromisoformat(last_updated_raw) if isinstance(last_updated_raw, str) else None
    )
    phases: list[RepoCheckpointPhase] = []
    for ph in PHASE_ORDER:
        label = _PHASE_LABELS.get(ph, ph)
        units = cp.get_done_units(ph)
        phases.append(
            RepoCheckpointPhase(
                phase=ph,
                label=label,
                done=ph in completed,
                units_done=len(units),
            )
        )
    return BifrostEnvelope(
        data=RepoCheckpointStatus(
            repo_id=repo_id,
            completed_phases=completed,
            resume_phase=cp.get_resume_phase(),
            last_updated=last_updated,
            phases=phases,
        )
    )
