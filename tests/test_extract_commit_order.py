"""Extraction routes must commit the task row BEFORE handing it to the worker.

``get_repository`` commits only after the response is sent, so a ``submit``
inside the handler used to race the worker's separate session ("Task not
found"). These tests drive the routes with a real committing session and
record the order of the commit and the submit.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.api.app import create_app
from bifrost.api.deps import get_repository
from bifrost.extraction import worker as worker_mod
from bifrost.storage.models import Domain, ExtractionTask, Repo
from bifrost.storage.repository import EntityRepository

TAG = uuid.uuid4().hex[:8]


class _FakeWorker:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.submitted: list[str] = []

    def submit(self, task_id: str) -> None:
        self.events.append("submit")
        self.submitted.append(task_id)


@pytest.fixture
def events() -> list[str]:
    return []


@pytest.fixture
def fake_worker(events: list[str]):
    prior = worker_mod._worker
    fake = _FakeWorker(events)
    worker_mod._worker = fake  # type: ignore[assignment]
    yield fake
    worker_mod._worker = prior


@pytest.fixture
async def factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def client(
    factory: async_sessionmaker[AsyncSession], events: list[str]
) -> AsyncGenerator[AsyncClient]:
    app = create_app()

    async def _real_repo():
        async with factory() as session:
            event.listen(session.sync_session, "after_commit", lambda _s: events.append("commit"))
            repo = EntityRepository(session)
            try:
                yield repo
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_repository] = _real_repo
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def committed_repo(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[Repo]:
    async with factory() as s:
        r = Repo(
            id=uuid.uuid4(),
            urn=f"urn:bifrost:repo:commit-order-{TAG}",
            name=f"commit-order-{TAG}",
            url=f"https://example.invalid/commit-order-{TAG}.git",
            adapter_config={},
        )
        s.add(r)
        await s.commit()
    yield r
    async with factory() as s:
        await s.execute(delete(Repo).where(Repo.id == r.id))
        await s.commit()


@pytest.fixture
async def cleanup_tasks(
    factory: async_sessionmaker[AsyncSession], fake_worker: _FakeWorker
) -> AsyncGenerator[None]:
    yield
    async with factory() as s:
        ids = [uuid.UUID(t) for t in fake_worker.submitted]
        if ids:
            await s.execute(delete(ExtractionTask).where(ExtractionTask.id.in_(ids)))
            await s.commit()


async def _post(client: AsyncClient, kind: str, repo: Repo, **ctx: Any):
    if kind == "repos-extract":
        return await client.post(f"/api/v1/repos/{repo.id}/extract", json={})
    if kind == "extract":
        return await client.post(f"/api/v1/extract?repo_id={repo.id}", json={})
    if kind == "extraction-tasks":
        return await client.post(
            "/api/v1/extraction-tasks",
            json={
                "type": "extraction",
                "query": "q",
                "scope": {"repo_ids": [str(repo.id)]},
            },
        )
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["repos-extract", "extract", "extraction-tasks"])
async def test_task_is_committed_before_submit(
    kind: str,
    client: AsyncClient,
    committed_repo: Repo,
    events: list[str],
    fake_worker: _FakeWorker,
    cleanup_tasks: None,
) -> None:
    resp = await _post(client, kind, committed_repo)
    assert resp.status_code == 202, resp.text
    assert events[:2] == ["commit", "submit"], events
    assert len(fake_worker.submitted) == 1


async def test_discover_repos_commits_before_submit(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    events: list[str],
    fake_worker: _FakeWorker,
    cleanup_tasks: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bifrost.manifest.team_repos import TeamRepoMatch

    domain_id = uuid.uuid4()
    async with factory() as s:
        s.add(Domain(id=domain_id, urn=f"urn:bifrost:domain:co-{TAG}", name=f"co-{TAG}"))
        await s.commit()
    events.clear()

    matches = [
        TeamRepoMatch(
            owner="acme",
            name=f"co-{TAG}-{i}",
            full_name=f"acme/co-{TAG}-{i}",
            git_url=f"https://example.invalid/acme/co-{TAG}-{i}.git",
            description="d",
            team="orders",
            providers=["backstage"],
        )
        for i in range(2)
    ]

    async def fake_discover(team: str, **kwargs: Any):
        return list(matches)

    monkeypatch.setattr("bifrost.api.routes.domains.discover_team_repos", fake_discover)

    try:
        resp = await client.post(
            f"/api/v1/domains/{domain_id}/discover-repos",
            json={"team": "orders", "trigger_extraction": True},
        )
        assert resp.status_code == 200, resp.text
        assert events[:3] == ["commit", "submit", "submit"], events
        assert len(fake_worker.submitted) == 2
    finally:
        async with factory() as s:
            await s.execute(delete(Repo).where(Repo.domain_id == domain_id))
            await s.execute(delete(Domain).where(Domain.id == domain_id))
            await s.commit()


async def test_register_repo_commits_before_background_spawn(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    events: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bifrost.api.routes import repos as repos_route

    def fake_spawn(coro: Any, *, what: str) -> None:
        coro.close()
        events.append("spawn")

    monkeypatch.setattr(repos_route, "spawn_background", fake_spawn)
    name = f"reg-{TAG}"
    try:
        resp = await client.post(
            "/api/v1/repos", json={"name": name, "url": "https://example.invalid/reg.git"}
        )
        assert resp.status_code == 201, resp.text
        assert events[:2] == ["commit", "spawn"], events
    finally:
        async with factory() as s:
            await s.execute(delete(Repo).where(Repo.urn == f"urn:bifrost:repo:{name}"))
            await s.commit()


async def test_update_task_status_raises_on_missing_task(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    w = worker_mod.ExtractionWorker.__new__(worker_mod.ExtractionWorker)
    w._session_factory = factory
    with pytest.raises(LookupError):
        await w._update_task_status(str(uuid.uuid4()), "running")
