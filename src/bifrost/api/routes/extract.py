"""Extraction trigger route handler."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from bifrost.api.deps import AdminOnly, Repo
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.api.schemas.repos import TriggerExtractionData, TriggerExtractionRequest

router = APIRouter(prefix="/extract", tags=["extract"])


@router.post(
    "",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[TriggerExtractionData],
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_extraction(
    body: TriggerExtractionRequest,
    repo: Repo,
    repo_id: str | None = None,
) -> BifrostEnvelope[TriggerExtractionData]:
    """Trigger extraction for a repository by repo_id query param.

    Note: The primary extraction trigger is POST /repos/{repo_id}/extract.
    This route exists as a convenience for triggering extraction without
    knowing the repo management URL structure.
    """
    if repo_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="repo_id query parameter is required",
        )

    db_repo = await repo.get_repo(uuid.UUID(repo_id))
    if db_repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found")

    scope: dict = {"repo_ids": [str(db_repo.id)], "force_full": body.force_full}
    if body.max_files is not None:
        scope["max_files"] = int(body.max_files)

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
