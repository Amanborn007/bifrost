"""Manifest sync must store the contact data the ownership lookups read.

``GET /api/v1/ownership`` and the handover generator both look for the first of
``contact`` / ``owner`` / ``maintainer`` / ``oncall`` in ``repo_ownership.raw_manifest``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.api.app import create_app
from bifrost.api.deps import get_db, get_repository
from bifrost.manifest.model import ServiceManifest
from bifrost.research.ownership_sync import manifest_to_ownership_values
from bifrost.storage.models import Repo, RepoOwnership
from bifrost.storage.repository import EntityRepository

LOOKUP_KEYS = ("contact", "owner", "maintainer", "oncall")


def _raw(**fields: Any) -> dict[str, Any]:
    md = ServiceManifest(name="svc", providers=["generic_yaml"], source_files=["svc.yml"], **fields)
    return manifest_to_ownership_values(uuid.uuid4(), md)["raw_manifest"]


def _first_contact(raw: dict[str, Any]) -> Any:
    return next((raw[k] for k in LOOKUP_KEYS if raw.get(k)), None)


def test_provenance_is_kept() -> None:
    raw = _raw(owners=["alice"])
    assert raw["providers"] == ["generic_yaml"]
    assert raw["files"] == ["svc.yml"]


def test_owners_become_the_owner_contact() -> None:
    assert _first_contact(_raw(owners=["alice", "bob"])) == "alice, bob"


def test_team_is_the_owner_when_there_are_no_owners() -> None:
    assert _first_contact(_raw(team="orders")) == "orders"


def test_slack_channel_is_the_contact_when_nothing_else_is_known() -> None:
    assert _first_contact(_raw(slack_channel="#order-eng")) == "#order-eng"


def test_manifest_without_contact_data_has_no_contact() -> None:
    assert _first_contact(_raw()) is None


def test_explicit_contact_fields_in_a_yaml_manifest_win() -> None:
    raw = _raw(
        owners=["alice"],
        slack_channel="#order-eng",
        raw={"generic_yaml": {"contact": "ops@example.com", "oncall": "pager-1"}},
    )
    assert raw["contact"] == "ops@example.com"
    assert raw["oncall"] == "pager-1"
    assert _first_contact(raw) == "ops@example.com"


def test_backstage_contact_annotation_is_carried() -> None:
    raw = _raw(
        raw={"backstage": {"metadata": {"annotations": {"example.com/oncall": "team-pager"}}}},
    )
    assert raw["oncall"] == "team-pager"
    assert _first_contact(raw) == "team-pager"


@pytest.fixture(autouse=True)
def _stub_worker():
    from bifrost.extraction import worker as _w

    prior = _w._worker
    _w._worker = MagicMock(spec=_w.ExtractionWorker)
    yield
    _w._worker = prior


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    app = create_app()

    async def _override_db():
        yield db_session

    async def _override_repo():
        yield EntityRepository(db_session)

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_repository] = _override_repo
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def test_synced_manifest_feeds_the_ownership_route(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    repo = Repo(
        id=uuid.uuid4(),
        urn="urn:bifrost:repo:contact-sync-svc",
        name="contact-sync-svc",
        url="https://example.com/acme/contact-sync-svc",
        adapter_config={},
    )
    db_session.add(repo)
    await db_session.flush()
    md = ServiceManifest(
        name="contact-sync-svc",
        owners=["alice", "bob"],
        providers=["generic_yaml"],
        source_files=["svc.yml"],
    )
    db_session.add(RepoOwnership(**manifest_to_ownership_values(repo.id, md)))
    await db_session.flush()

    resp = await client.get("/api/v1/ownership?q=contact-sync-svc")
    assert resp.status_code == 200
    results = resp.json()["data"]["results"]
    assert [r["primary_contact"] for r in results if r["repo_name"] == "contact-sync-svc"] == [
        "alice, bob"
    ]
