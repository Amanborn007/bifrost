"""CSRF middleware: mutating requests from a browser must come from a
configured CORS origin and carry ``X-Requested-With: bifrost-ui``."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from bifrost.cli.config import reset_settings

ALLOWED = "http://localhost:5173"
MUTATING = ["POST", "PUT", "PATCH", "DELETE"]
UI = {"X-Requested-With": "bifrost-ui"}


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("BIFROST_CORS_ORIGINS", f"{ALLOWED},https://bifrost.example.com")
    reset_settings()
    from bifrost.api.app import create_app

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        yield ac
    reset_settings()


async def _logout(ac: AsyncClient, method: str = "POST", **headers: str) -> int:
    resp = await ac.request(method, "/api/v1/auth/logout", headers=headers)
    return resp.status_code


@pytest.mark.parametrize("method", MUTATING)
async def test_cross_origin_mutation_rejected(client: AsyncClient, method: str) -> None:
    resp = await client.request(
        method,
        "/api/v1/auth/logout",
        headers={"Origin": "https://evil.example.com", **UI},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "csrf_origin"


async def test_same_origin_with_header_accepted(client: AsyncClient) -> None:
    assert await _logout(client, Origin=ALLOWED, **UI) == 200
    assert await _logout(client, Origin="https://bifrost.example.com", **UI) == 200


async def test_allowed_origin_without_header_rejected(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/logout", headers={"Origin": ALLOWED})
    assert resp.status_code == 403
    assert resp.json()["code"] == "csrf_header"


async def test_wrong_header_value_rejected(client: AsyncClient) -> None:
    status = await _logout(client, Origin=ALLOWED, **{"X-Requested-With": "XMLHttpRequest"})
    assert status == 403


async def test_cookie_auth_without_header_rejected(client: AsyncClient) -> None:
    status = await _logout(client, Cookie="bifrost_session=abc")
    assert status == 403
    assert await _logout(client, Cookie="bifrost_session=abc", **UI) == 200


async def test_sec_fetch_site_used_when_origin_absent(client: AsyncClient) -> None:
    assert await _logout(client, **{"Sec-Fetch-Site": "cross-site", **UI}) == 403
    assert await _logout(client, **{"Sec-Fetch-Site": "same-site", **UI}) == 403
    assert await _logout(client, **{"Sec-Fetch-Site": "same-origin", **UI}) == 200
    assert await _logout(client, **{"Sec-Fetch-Site": "same-origin"}) == 403


async def test_bearer_clients_are_exempt(client: AsyncClient) -> None:
    assert await _logout(client, Authorization="Bearer abc") == 200
    assert (
        await _logout(client, Authorization="Bearer abc", Origin="https://evil.example.com") == 200
    )


async def test_non_browser_client_without_signals_is_not_blocked(client: AsyncClient) -> None:
    assert await _logout(client) == 200


async def test_get_untouched(client: AsyncClient) -> None:
    resp = await client.get("/health", headers={"Origin": "https://evil.example.com"})
    assert resp.status_code == 200


async def test_preflight_untouched(client: AsyncClient) -> None:
    resp = await client.options(
        "/api/v1/auth/logout",
        headers={"Origin": ALLOWED, "Access-Control-Request-Method": "POST"},
    )
    assert resp.status_code == 200


async def test_csrf_rejection_carries_cors_headers_for_allowed_origin(
    client: AsyncClient,
) -> None:
    resp = await client.post("/api/v1/auth/logout", headers={"Origin": ALLOWED})
    assert resp.status_code == 403
    assert resp.headers["access-control-allow-origin"] == ALLOWED
