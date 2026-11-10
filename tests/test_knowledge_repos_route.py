"""GET /knowledge/repos returns per-repo ``queries_last_7d`` from conversations."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.api.app import create_app
from bifrost.api.deps import get_db
from bifrost.storage.models import Conversation, Repo


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    app = create_app()

    async def _db() -> AsyncGenerator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = _db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def _repo(db: AsyncSession, name: str) -> Repo:
    repo = Repo(
        id=uuid.uuid4(),
        urn=f"urn:bifrost:repo:{name}",
        name=name,
        url=f"https://github.com/test/{name}",
        adapter_config={},
    )
    db.add(repo)
    await db.flush()
    return repo


def _conv(repo_ids: list[uuid.UUID], *, age_days: int = 0) -> Conversation:
    when = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=age_days)
    return Conversation(
        id=uuid.uuid4(),
        session_id="s",
        title="t",
        scope_repo_ids=[str(r) for r in repo_ids],
        created_at=when,
        updated_at=when,
    )


async def _row(client: AsyncClient, repo: Repo) -> dict:
    resp = await client.get("/api/v1/knowledge/repos")
    assert resp.status_code == 200, resp.text
    rows = [r for r in resp.json()["data"]["repos"] if r["id"] == str(repo.id)]
    assert len(rows) == 1
    return rows[0]


async def test_repos_route_zero_queries_when_no_conversations(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    repo = await _repo(db_session, "kn-zero")
    assert (await _row(client, repo))["queries_last_7d"] == 0


async def test_repos_route_counts_recent_scoped_conversations(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    repo = await _repo(db_session, "kn-counted")
    other = await _repo(db_session, "kn-other")
    db_session.add_all(
        [
            _conv([repo.id]),
            _conv([repo.id, other.id], age_days=2),
            _conv([repo.id], age_days=30),  # outside the 7 day window
            _conv([other.id]),
        ]
    )
    await db_session.flush()

    assert (await _row(client, repo))["queries_last_7d"] == 2
    assert (await _row(client, other))["queries_last_7d"] == 2
