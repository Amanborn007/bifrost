"""SSE streaming route for task progress per TRD §4.4."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from bifrost.api.deps import Repo
from bifrost.api.sse import event_stream

router = APIRouter(prefix="/tasks", tags=["streaming"])


@router.get("/{task_id}/stream")
async def stream_task(task_id: str, repo: Repo) -> StreamingResponse:
    """Server-Sent Events stream for task progress.

    Content-Type: text/event-stream.
    Each SSE frame: `event: {type}\\ndata: {JSON}\\n\\n`

    The bus is one shared key namespace, so the id must name an extraction
    task row; conversation, collab and handover keys are never reachable here.
    """
    try:
        tid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    if await repo.get_task(tid) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return StreamingResponse(
        event_stream(str(tid)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
