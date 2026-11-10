"""discover-repos applies the sources.local.allowed_roots check before creating Repo rows."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.api.app import create_app
from bifrost.api.deps import get_db, get_repository
from bifrost.cli.config import get_settings
from bifrost.cli.config_sections import LocalSourceConfig, SourcesConfig
from bifrost.manifest.team_repos import TeamRepoMatch
from bifrost.storage.models import Domain, Repo
from bifrost.storage.repository import EntityRepository

TAG = uuid.uuid4().hex[:8]


@pytest.fixture(autouse=True)
def _stub_worker():
    from bifrost.extraction import worker as _w

    prior = _w._worker
    _w._worker = MagicMock(spec=_w.ExtractionWorker)
    yield _w._worker
    _w._worker = prior


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
async def domain(db_session: AsyncSession) -> Domain:
    d = Domain(id=uuid.uuid4(), urn=f"urn:bifrost:domain:dlvr-{TAG}", name=f"dlvr-{TAG}")
    db_session.add(d)
    await db_session.flush()
    return d


def _match(name: str, git_url: str) -> TeamRepoMatch:
    return TeamRepoMatch(
        owner="acme",
        name=name,
        full_name=f"acme/{name}",
        git_url=git_url,
        description=None,
        team="dlvr",
        providers=["backstage"],
    )


def _use(app: Any, monkeypatch: pytest.MonkeyPatch, matches: list[TeamRepoMatch], **update: Any):
    patched = get_settings().model_copy(update=update)
    app.dependency_overrides[get_settings] = lambda: patched

    async def fake(team: str, **kwargs: Any):
        return list(matches)

    monkeypatch.setattr("bifrost.api.routes.domains.discover_team_repos", fake)


async def _repo_names(db: AsyncSession, *names: str) -> set[str]:
    rows = await db.execute(select(Repo.name).where(Repo.name.in_(names)))
    return set(rows.scalars())


async def test_local_path_outside_roots_is_reported_and_not_created(
    app,
    client: AsyncClient,
    db_session: AsyncSession,
    domain: Domain,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = f"loc-outside-{TAG}"
    inside = f"loc-inside-{TAG}"
    _use(
        app,
        monkeypatch,
        [_match(outside, str(tmp_path / "elsewhere")), _match(inside, str(root / "svc"))],
        sources=SourcesConfig(local=LocalSourceConfig(allowed_roots=[str(root)])),
    )

    resp = await client.post(f"/api/v1/domains/{domain.id}/discover-repos", json={"team": "dlvr"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert [e["full_name"] for e in data["errors"]] == [f"acme/{outside}"]
    assert "allowed_roots" in data["errors"][0]["error"]
    assert [e["full_name"] for e in data["added"]] == [f"acme/{inside}"]
    assert await _repo_names(db_session, outside, inside) == {inside}


async def test_production_without_roots_refuses_local_paths(
    app,
    client: AsyncClient,
    db_session: AsyncSession,
    domain: Domain,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = f"loc-prod-{TAG}"
    _use(app, monkeypatch, [_match(name, str(tmp_path))], production_mode=True)

    resp = await client.post(
        f"/api/v1/domains/{domain.id}/discover-repos", json={"team": "dlvr", "dry_run": True}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data["errors"]) == 1
    assert data["added"] == []
    assert await _repo_names(db_session, name) == set()


async def test_remote_urls_unaffected_in_production(
    app,
    client: AsyncClient,
    db_session: AsyncSession,
    domain: Domain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = f"gh-prod-{TAG}"
    _use(
        app,
        monkeypatch,
        [_match(name, f"https://github.com/acme/{name}.git")],
        production_mode=True,
    )

    resp = await client.post(f"/api/v1/domains/{domain.id}/discover-repos", json={"team": "dlvr"})
    assert resp.status_code == 200, resp.text
    assert [e["full_name"] for e in resp.json()["data"]["added"]] == [f"acme/{name}"]
