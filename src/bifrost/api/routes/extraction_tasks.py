"""Task status and history route handlers per TRD §4.3."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status

from bifrost.api.deps import AdminOnly, Repo
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.api.schemas.extraction_tasks import (
    CancelTaskData,
    SubmitTaskData,
    SubmitTaskRequest,
    TaskActionData,
    TaskDetail,
    TaskSummary,
)

router = APIRouter(prefix="/extraction-tasks", tags=["tasks"])


@router.post(
    "",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[SubmitTaskData],
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_task(
    body: SubmitTaskRequest,
    repo: Repo,
) -> BifrostEnvelope[SubmitTaskData]:
    """Submit a new task for orchestrated processing."""
    task = await repo.create_task(
        task_type=body.type,
        query=body.query,
        scope_json=body.scope.model_dump(),
        budget_json=body.budget.model_dump() if body.budget else {},
    )
    await repo.commit()

    from bifrost.extraction.worker import get_worker

    get_worker().submit(str(task.id))

    return BifrostEnvelope(
        data=SubmitTaskData(
            task_id=str(task.id),
            stream_url=f"/api/v1/tasks/{task.id}/stream",
        ),
    )


@router.get("", response_model=BifrostEnvelope[list[TaskSummary]])
async def list_tasks(
    repo: Repo,
    task_status: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> BifrostEnvelope[list[TaskSummary]]:
    """List tasks with optional status filter."""
    tasks = await repo.list_tasks(status=task_status, limit=limit, offset=offset)
    return BifrostEnvelope(
        data=[
            TaskSummary(
                id=str(t.id),
                type=t.task_type,
                status=t.status,
                query=t.query,
                created_at=t.created_at,
                updated_at=t.updated_at,
                result=t.result_json if t.result_json else None,
                error_message=t.error_message,
            )
            for t in tasks
        ],
    )


@router.get("/{task_id}", response_model=BifrostEnvelope[TaskDetail])
async def get_task(task_id: str, repo: Repo) -> BifrostEnvelope[TaskDetail]:
    """Get task detail including result when available."""
    task = await repo.get_task(uuid.UUID(task_id))
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    return BifrostEnvelope(
        data=TaskDetail(
            id=str(task.id),
            type=task.task_type,
            status=task.status,
            query=task.query,
            result=task.result_json,
            created_at=task.created_at,
            updated_at=task.updated_at,
        ),
    )


@router.delete(
    "/{task_id}", dependencies=[AdminOnly], response_model=BifrostEnvelope[CancelTaskData]
)
async def cancel_task(task_id: str, repo: Repo) -> BifrostEnvelope[CancelTaskData]:
    """Cancel a running task."""
    task = await repo.get_task(uuid.UUID(task_id))
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    terminal_states = {"delivered", "failed", "cancelled"}
    if task.status in terminal_states:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Task already in terminal state: {task.status}",
        )

    from bifrost.extraction.worker import get_worker

    worker = get_worker()
    if not worker.cancel(task_id):
        # Not tracked by worker — update DB directly
        task.status = "cancelled"
        # session commits via deps.py get_repository context manager

    return BifrostEnvelope(data=CancelTaskData(id=str(task.id)))


@router.post(
    "/{task_id}/pause", dependencies=[AdminOnly], response_model=BifrostEnvelope[TaskActionData]
)
async def pause_task(task_id: str, repo: Repo) -> BifrostEnvelope[TaskActionData]:
    """Pause a running extraction task."""
    task = await repo.get_task(uuid.UUID(task_id))
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task.status != "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Can only pause running tasks, current status: {task.status}",
        )

    from bifrost.extraction.worker import get_worker

    if not get_worker().pause(task_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task not tracked by worker",
        )

    return BifrostEnvelope(data=TaskActionData(id=task_id, status="paused"))


@router.post(
    "/{task_id}/resume", dependencies=[AdminOnly], response_model=BifrostEnvelope[TaskActionData]
)
async def resume_task(task_id: str, repo: Repo) -> BifrostEnvelope[TaskActionData]:
    """Resume a paused extraction task."""
    task = await repo.get_task(uuid.UUID(task_id))
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task.status != "paused":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Can only resume paused tasks, current status: {task.status}",
        )

    from bifrost.extraction.worker import get_worker

    if not get_worker().resume(task_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task not tracked by worker",
        )

    return BifrostEnvelope(data=TaskActionData(id=task_id, status="running"))
