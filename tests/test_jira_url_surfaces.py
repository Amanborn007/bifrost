"""Jira links are built from ``sources.jira.base_url``; with no base URL they are null.

Every API surface that carries a ``jira_key`` also carries ``jira_url``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.api.app import create_app
from bifrost.api.deps import get_db, get_repository
from bifrost.cli.config import reset_settings
from bifrost.storage.repository import EntityRepository

BASE_URL = "https://acme.atlassian.net"
KEY = "ABC-4321"


@pytest.fixture(autouse=True)
def _stub_worker():
    from unittest.mock import MagicMock

    from bifrost.extraction import worker as _w

    prior = _w._worker
    _w._worker = MagicMock(spec=_w.ExtractionWorker)
    yield
    _w._worker = prior


@pytest.fixture
def jira_base(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BIFROST_SOURCES__JIRA__BASE_URL", raising=False)
    reset_settings()

    def _set(url: str | None) -> None:
        if url is None:
            monkeypatch.delenv("BIFROST_SOURCES__JIRA__BASE_URL", raising=False)
        else:
            monkeypatch.setenv("BIFROST_SOURCES__JIRA__BASE_URL", url)
        reset_settings()

    yield _set
    monkeypatch.undo()
    reset_settings()


@pytest.fixture
def app(db_session: AsyncSession):
    application = create_app()

    async def _override_db():
        yield db_session

    async def _override_repo():
        yield EntityRepository(db_session)

    application.dependency_overrides[get_db] = _override_db
    application.dependency_overrides[get_repository] = _override_repo
    return application


@pytest.fixture
async def client(app) -> AsyncGenerator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def seeded(db_session: AsyncSession) -> dict[str, str]:
    task_id = str(uuid.uuid4())
    await db_session.execute(
        text(
            "INSERT INTO tasks (id, title, intent_text, jira_key) "
            "VALUES (CAST(:id AS uuid), 'jira url surface', 'i', :k)"
        ),
        {"id": task_id, "k": KEY},
    )
    conv_id = str(
        (
            await db_session.execute(
                text(
                    "INSERT INTO conversations (session_id, title) "
                    "VALUES ('sess-jira-url', 'c') RETURNING id::text"
                )
            )
        ).scalar_one()
    )
    await db_session.execute(
        text(
            "INSERT INTO conversation_task_links (conversation_id, task_id, link_type) "
            "VALUES (CAST(:c AS uuid), CAST(:t AS uuid), 'spawned_brief')"
        ),
        {"c": conv_id, "t": task_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO task_jira_audit (task_id, action, jira_key, ok, actor) "
            "VALUES (CAST(:t AS uuid), 'create', :k, true, 'test')"
        ),
        {"t": task_id, "k": KEY},
    )
    session_id = str(
        (
            await db_session.execute(
                text(
                    "INSERT INTO collaboration_sessions (requester, requirement_text) "
                    "VALUES ('someone@example.com', 'req') RETURNING id::text"
                )
            )
        ).scalar_one()
    )
    await db_session.execute(
        text(
            "INSERT INTO session_task_links (session_id, task_id) "
            "VALUES (CAST(:s AS uuid), CAST(:t AS uuid))"
        ),
        {"s": session_id, "t": task_id},
    )
    await db_session.flush()
    return {"task": task_id, "conv": conv_id, "session": session_id}


def _find(items: list[dict[str, Any]]) -> dict[str, Any]:
    assert len(items) == 1, items
    return items[0]


# surface name -> (path builder, picks the row that carries the seeded task)
SURFACES: dict[str, tuple[Callable[[dict[str, str]], str], Callable[[Any], dict[str, Any]]]] = {
    "tasks list": (
        lambda ids: f"/api/v1/tasks?q={KEY}",
        lambda data: _find(data),
    ),
    "task detail": (lambda ids: f"/api/v1/tasks/{ids['task']}", lambda data: data),
    "conversation tasks": (
        lambda ids: f"/api/v1/conversations/{ids['conv']}/tasks",
        lambda data: _find(data),
    ),
    "conversation connections": (
        lambda ids: f"/api/v1/conversations/{ids['conv']}/connections",
        lambda data: _find(data["tasks"]),
    ),
    "jira audit": (
        lambda ids: f"/api/v1/tasks/{ids['task']}/jira-audit",
        lambda data: _find(data),
    ),
    "collab session tasks": (
        lambda ids: f"/api/v1/collab-sessions/{ids['session']}/tasks",
        lambda data: _find(data),
    ),
}


@pytest.mark.parametrize("surface", list(SURFACES))
async def test_jira_url_is_null_by_default(
    surface: str, client: AsyncClient, seeded: dict[str, str], jira_base
) -> None:
    jira_base(None)
    build, pick = SURFACES[surface]
    resp = await client.get(build(seeded))
    assert resp.status_code == 200, resp.text
    row = pick(resp.json()["data"])
    assert row["jira_key"] == KEY
    assert "jira_url" in row
    assert row["jira_url"] is None


@pytest.mark.parametrize("surface", list(SURFACES))
async def test_jira_url_is_the_full_browse_url_when_base_url_is_set(
    surface: str, client: AsyncClient, seeded: dict[str, str], jira_base
) -> None:
    jira_base(BASE_URL + "/")
    build, pick = SURFACES[surface]
    resp = await client.get(build(seeded))
    assert resp.status_code == 200, resp.text
    row = pick(resp.json()["data"])
    assert row["jira_url"] == f"{BASE_URL}/browse/{KEY}"
