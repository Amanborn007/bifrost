"""Stream-route authorization: /tasks/{id}/stream must not reach other bus keys.

The SSE bus is one shared namespace (task ids, conversation ids, collab
session ids, ``handover:<kind>:<value>``). Each stream route may only
subscribe to keys of its own kind.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.api.app import create_app
from bifrost.api.deps import get_repository
from bifrost.api.sse import SSEEvent, event_bus
from bifrost.collaboration.events import handover_channel_key
from bifrost.storage.models import CollaborationSession, Conversation, ExtractionTask
from bifrost.storage.repository import EntityRepository


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    app = create_app()

    async def _override_repo():
        yield EntityRepository(db_session)

    app.dependency_overrides[get_repository] = _override_repo
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def _get(client: AsyncClient, path: str):
    """GET with a deadline: a stream that never ends fails the test, not the run."""
    async with asyncio.timeout(5):
        return await client.get(path)


async def _finished_stream(key: str, marker: str) -> None:
    """Publish one event then close, so a late subscriber replays it and ends."""
    await event_bus.publish(key, SSEEvent(event_type="task_complete", data={"marker": marker}))
    await event_bus.close(key)


async def test_conversation_uuid_not_reachable_via_task_stream(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    conv = Conversation(id=uuid.uuid4(), session_id="someone-else", title="t")
    db_session.add(conv)
    await db_session.flush()
    await _finished_stream(str(conv.id), "conv-secret")

    resp = await _get(client, f"/api/v1/tasks/{conv.id}/stream")

    assert resp.status_code == 404
    assert "conv-secret" not in resp.text


async def test_handover_key_not_reachable_via_task_stream(client: AsyncClient) -> None:
    key = handover_channel_key("repo", str(uuid.uuid4()))
    await _finished_stream(key, "handover-secret")

    resp = await _get(client, f"/api/v1/tasks/{key}/stream")

    assert resp.status_code == 404
    assert "handover-secret" not in resp.text


async def test_non_uuid_task_id_is_404(client: AsyncClient) -> None:
    resp = await _get(client, "/api/v1/tasks/not-a-uuid/stream")
    assert resp.status_code == 404


async def test_unknown_uuid_task_id_is_404(client: AsyncClient) -> None:
    resp = await _get(client, f"/api/v1/tasks/{uuid.uuid4()}/stream")
    assert resp.status_code == 404


async def test_extraction_task_stream_delivers_its_event(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    task = ExtractionTask(
        id=uuid.uuid4(),
        task_type="question",
        query="q",
        scope_json={"repo_ids": []},
        budget_json={},
        status="running",
    )
    db_session.add(task)
    await db_session.flush()
    await _finished_stream(str(task.id), "task-event")

    resp = await _get(client, f"/api/v1/tasks/{task.id}/stream")

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "task-event" in resp.text


async def test_unknown_collab_session_stream_is_404(client: AsyncClient) -> None:
    resp = await _get(client, f"/api/v1/collab-sessions/{uuid.uuid4()}/stream")
    assert resp.status_code == 404


async def test_known_collab_session_stream_is_200(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    sess = CollaborationSession(id=uuid.uuid4(), requester="a@b.c", requirement_text="r")
    db_session.add(sess)
    await db_session.flush()
    await _finished_stream(str(sess.id), "collab-event")

    resp = await _get(client, f"/api/v1/collab-sessions/{sess.id}/stream")

    assert resp.status_code == 200
    assert "collab-event" in resp.text
