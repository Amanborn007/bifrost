"""POST /repos: token-shaped keys must use the encrypted namespace and are never echoed."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.api.app import create_app
from bifrost.api.deps import get_repository
from bifrost.cli.config import reset_settings
from bifrost.core.crypto import CIPHERTEXT_PREFIX, RESERVED_PREFIX
from bifrost.storage.repository import EntityRepository

SECRET = "ghp_plaintext_must_never_hit_disk"


@pytest.fixture(autouse=True)
def _credential_key(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    monkeypatch.setenv("BIFROST_CREDENTIAL_KEY", Fernet.generate_key().decode())
    reset_settings()
    yield
    monkeypatch.undo()
    reset_settings()


@pytest.fixture(autouse=True)
def _stub_worker():
    from unittest.mock import MagicMock

    from bifrost.extraction import worker as _w

    prior = _w._worker
    _w._worker = MagicMock(spec=_w.ExtractionWorker)
    yield
    _w._worker = prior


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    application = create_app()
    db_session.commit = db_session.flush  # routes now commit; keep the test rollback-only

    async def _override_repo():
        yield EntityRepository(db_session)

    application.dependency_overrides[get_repository] = _override_repo
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as ac:
        yield ac


def _payload(name: str, adapter_config: dict[str, str]) -> dict[str, object]:
    return {"name": name, "url": "https://git.example.com/org/r", "adapter_config": adapter_config}


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "github_token",
        "API_KEY",
        "apiKey",
        "password",
        "client_secret",
        "pat",
        "credential",
    ],
)
async def test_token_shaped_key_outside_namespace_is_rejected(
    client: AsyncClient, key: str
) -> None:
    resp = await client.post("/api/v1/repos", json=_payload("bad-key", {key: SECRET}))
    assert resp.status_code == 400
    assert RESERVED_PREFIX in resp.text
    assert SECRET not in resp.text


@pytest.mark.parametrize("key", ["squad", "clone_path", "patch_level", "description"])
async def test_ordinary_keys_are_accepted(client: AsyncClient, key: str) -> None:
    resp = await client.post("/api/v1/repos", json=_payload(f"ok-{key}", {key: "v"}))
    assert resp.status_code == 201


async def test_namespaced_secret_is_accepted_and_redacted_in_response(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/repos", json=_payload("good", {f"{RESERVED_PREFIX}token": SECRET, "squad": "x"})
    )
    assert resp.status_code == 201
    assert SECRET not in resp.text
    assert resp.json()["data"]["adapter_config"] == {f"{RESERVED_PREFIX}token": "***", "squad": "x"}


async def test_database_holds_ciphertext_not_plaintext(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    resp = await client.post(
        "/api/v1/repos", json=_payload("at-rest", {f"{RESERVED_PREFIX}token": SECRET})
    )
    assert resp.status_code == 201
    raw = (
        await db_session.execute(
            text("SELECT adapter_config::text FROM repos WHERE name = 'at-rest'")
        )
    ).scalar_one()
    assert SECRET not in raw
    assert CIPHERTEXT_PREFIX in raw


async def test_orm_read_decrypts(client: AsyncClient, db_session: AsyncSession) -> None:
    await client.post(
        "/api/v1/repos", json=_payload("orm-read", {f"{RESERVED_PREFIX}token": SECRET})
    )
    repo = await EntityRepository(db_session).get_repo_by_urn("urn:bifrost:repo:orm-read")
    assert repo is not None
    assert repo.adapter_config[f"{RESERVED_PREFIX}token"] == SECRET
