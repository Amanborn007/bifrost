"""POST /domains/{id}/discover-repos replaces the old team-sync endpoint."""

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
from bifrost.errors import UpstreamError
from bifrost.storage.models import Domain, Repo
from bifrost.storage.repository import EntityRepository
from tests import leak_terms

REPO_ROOT = Path(__file__).resolve().parent.parent
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
    db_session.commit = db_session.flush  # routes now commit; keep the test rollback-only

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
    d = Domain(id=uuid.uuid4(), urn=f"urn:bifrost:domain:orders-{TAG}", name=f"orders-{TAG}")
    db_session.add(d)
    await db_session.flush()
    return d


def _match(name: str, *, team: str = "orders"):
    from bifrost.manifest.team_repos import TeamRepoMatch

    return TeamRepoMatch(
        owner="acme",
        name=name,
        full_name=f"acme/{name}",
        git_url=f"https://github.com/acme/{name}.git",
        description=f"{name} description",
        team=team,
        providers=["backstage"],
    )


def _patch_discover(
    monkeypatch: pytest.MonkeyPatch, matches: list, seen: dict[str, Any] | None = None
):
    async def fake(team: str, **kwargs: Any):
        if seen is not None:
            seen["team"] = team
            seen["kwargs"] = kwargs
        return list(matches)

    monkeypatch.setattr("bifrost.api.routes.domains.discover_team_repos", fake)


async def _add_repo(db: AsyncSession, name: str, domain_id: uuid.UUID | None) -> Repo:
    r = Repo(
        id=uuid.uuid4(),
        urn=f"urn:bifrost:repo:acme:{name}-{TAG}",
        name=name,
        url=f"https://github.com/acme/{name}.git",
        domain_id=domain_id,
        adapter_config={},
    )
    db.add(r)
    await db.flush()
    return r


def _url(domain: Domain) -> str:
    return f"/api/v1/domains/{domain.id}/discover-repos"


async def test_dry_run_reports_without_writing(
    client: AsyncClient, db_session: AsyncSession, domain: Domain, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlinked = await _add_repo(db_session, f"unlinked-{TAG}", None)
    _patch_discover(monkeypatch, [_match(f"brand-new-{TAG}"), _match(f"unlinked-{TAG}")])

    resp = await client.post(_url(domain), json={"team": "orders", "dry_run": True})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["dry_run"] is True
    assert data["candidates_found"] == 2
    assert [e["full_name"] for e in data["added"]] == [f"acme/brand-new-{TAG}"]
    assert [e["full_name"] for e in data["linked"]] == [f"acme/unlinked-{TAG}"]
    assert data["added"][0]["repo_id"] is None

    created = (
        (await db_session.execute(select(Repo).where(Repo.name == f"brand-new-{TAG}")))
        .scalars()
        .all()
    )
    assert created == []
    await db_session.refresh(unlinked)
    assert unlinked.domain_id is None


async def test_adds_new_repos_and_links_existing_ones(
    client: AsyncClient, db_session: AsyncSession, domain: Domain, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlinked = await _add_repo(db_session, f"unlinked-{TAG}", None)
    mine = await _add_repo(db_session, f"mine-{TAG}", domain.id)
    other_domain = Domain(
        id=uuid.uuid4(), urn=f"urn:bifrost:domain:other-{TAG}", name=f"other-{TAG}"
    )
    db_session.add(other_domain)
    await db_session.flush()
    theirs = await _add_repo(db_session, f"theirs-{TAG}", other_domain.id)
    _patch_discover(
        monkeypatch,
        [
            _match(f"brand-new-{TAG}"),
            _match(f"unlinked-{TAG}"),
            _match(f"mine-{TAG}"),
            _match(f"theirs-{TAG}"),
        ],
    )

    resp = await client.post(_url(domain), json={"team": "orders"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["team"] == "orders"
    assert data["dry_run"] is False
    assert data["candidates_found"] == 4
    assert [e["action"] for e in data["added"]] == ["added"]
    assert [e["action"] for e in data["linked"]] == ["linked"]
    assert [e["action"] for e in data["already_linked"]] == ["already_linked"]
    assert [e["full_name"] for e in data["errors"]] == [f"acme/theirs-{TAG}"]
    assert data["errors"][0]["action"] == "error"
    assert "different domain" in data["errors"][0]["error"]

    new = (
        await db_session.execute(select(Repo).where(Repo.name == f"brand-new-{TAG}"))
    ).scalar_one()
    assert new.domain_id == domain.id
    assert new.url == f"https://github.com/acme/brand-new-{TAG}.git"
    assert new.adapter_config["team"] == "orders"
    assert "squad" not in new.adapter_config
    await db_session.refresh(unlinked)
    assert unlinked.domain_id == domain.id
    await db_session.refresh(theirs)
    assert theirs.domain_id == other_domain.id
    await db_session.refresh(mine)
    assert mine.domain_id == domain.id


async def test_trigger_extraction_queues_one_task_per_new_repo(
    client: AsyncClient, domain: Domain, monkeypatch: pytest.MonkeyPatch, _stub_worker: MagicMock
) -> None:
    _patch_discover(monkeypatch, [_match(f"brand-new-{TAG}")])
    resp = await client.post(_url(domain), json={"team": "orders", "trigger_extraction": True})
    assert resp.status_code == 200, resp.text
    entry = resp.json()["data"]["added"][0]
    assert entry["task_id"]
    _stub_worker.submit.assert_called_once_with(entry["task_id"])


async def test_team_defaults_to_the_domain_name(
    client: AsyncClient, domain: Domain, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    _patch_discover(monkeypatch, [], seen)
    resp = await client.post(_url(domain), json={})
    assert resp.status_code == 200, resp.text
    assert seen["team"] == domain.name
    assert resp.json()["data"]["team"] == domain.name
    assert seen["kwargs"]["settings"] is not None


async def test_response_lists_the_enabled_manifest_providers(
    client: AsyncClient, domain: Domain, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_discover(monkeypatch, [])
    data = (await client.post(_url(domain), json={"team": "orders"})).json()["data"]
    assert data["providers"] == ["backstage", "codeowners"]


async def test_unknown_domain_is_404(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_discover(monkeypatch, [])
    resp = await client.post(f"/api/v1/domains/{uuid.uuid4()}/discover-repos", json={})
    assert resp.status_code == 404


async def test_upstream_failure_surfaces_through_the_error_handler(
    client: AsyncClient, domain: Domain, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(team: str, **kwargs: Any):
        raise UpstreamError("gh search failed")

    monkeypatch.setattr("bifrost.api.routes.domains.discover_team_repos", boom)
    resp = await client.post(_url(domain), json={"team": "orders"})
    assert resp.status_code == 502


async def test_no_orgs_configured_is_a_400(client: AsyncClient, domain: Domain) -> None:
    resp = await client.post(_url(domain), json={"team": "orders"})
    assert resp.status_code == 400, resp.text


def test_no_route_path_carries_a_banned_name() -> None:
    paths = [getattr(r, "path", "") for r in create_app().routes]
    assert [p for p in paths if leak_terms.matches(p)] == []


# ── source guards ───────────────────────────────────────────────────────


def test_domains_route_names_no_banned_term_nor_github_com() -> None:
    text = (REPO_ROOT / "src/bifrost/api/routes/domains.py").read_text(encoding="utf-8")
    assert "github.com" not in text.lower()
    assert not leak_terms.matches(text)
