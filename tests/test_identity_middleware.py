"""End-to-end tests for the identity middleware: 401 envelope on
unauthed protected paths, public-allowlist passthrough, header-strip
defense-in-depth, demo-mode short-circuit."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def reset_identity_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip every env var the providers read + reset cached settings.
    Keeps each test isolated."""
    for k in (
        "BIFROST_OPERATOR_EMAIL",
        "BIFROST_OPERATOR_GROUPS",
        "BIFROST_DEMO_MODE",
        "BIFROST_IDENTITY__PROVIDER",
        "BIFROST_IDENTITY__TRUSTED_PROXY",
        "BIFROST_IDENTITY__ROLES",
    ):
        monkeypatch.delenv(k, raising=False)
    from bifrost.cli.config import reset_settings

    reset_settings()
    yield
    reset_settings()


def _build_app() -> object:
    from bifrost.api.app import create_app

    return create_app()


@pytest.mark.asyncio
async def test_health_is_public(reset_identity_env: None) -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_protected_path_rejects_unauthenticated(
    reset_identity_env: None,
) -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/v1/repos")
        assert r.status_code == 401
        body = r.json()
        assert body.get("error") == "missing identity"
        assert body.get("code") == "identity_required"


@pytest.mark.asyncio
async def test_auth_me_is_public_but_returns_401_when_no_principal(
    reset_identity_env: None,
) -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/v1/auth/me")
        # Public path → middleware lets it through, but the route
        # itself surfaces 401 + the same envelope so the FE gate
        # can branch into demo / error states.
        assert r.status_code == 401
        body = r.json()
        assert body.get("code") == "identity_required"


@pytest.mark.asyncio
async def test_env_override_resolves_principal(
    reset_identity_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIFROST_OPERATOR_EMAIL", "tester@example.com")
    monkeypatch.setenv("BIFROST_OPERATOR_GROUPS", "bifrost-engineer")
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/v1/auth/me")
        assert r.status_code == 200
        body = r.json()
        assert body["email"] == "tester@example.com"
        assert body["source"] == "env_override"
        assert "bifrost-engineer" in body["groups"]


@pytest.mark.asyncio
async def test_spoofed_sso_header_rejected_when_trust_off(
    reset_identity_env: None,
) -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get(
            "/api/v1/repos",
            headers={"X-Forwarded-Email": "spoof@evil.com"},
        )
        assert r.status_code == 401
        # The spoofed identity should NOT have made it through to a
        # 200 — the middleware strips X-Forwarded-* when trust is
        # not declared.


@pytest.mark.asyncio
async def test_trusted_sso_header_accepted_when_trust_on(
    reset_identity_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIFROST_IDENTITY__PROVIDER", "header")
    monkeypatch.setenv("BIFROST_IDENTITY__TRUSTED_PROXY", "true")
    monkeypatch.setenv("BIFROST_IDENTITY__ROLES", '{"admin": []}')
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get(
            "/api/v1/auth/me",
            headers={
                "X-Forwarded-Email": "real@example.com",
                "X-Forwarded-Groups": "eng,platform",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["email"] == "real@example.com"
        assert body["source"] == "sso_header"


@pytest.mark.asyncio
async def test_demo_mode_resolves_demo_principal(
    reset_identity_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIFROST_DEMO_MODE", "true")
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        # Demo mode → /me returns demo_fallback principal (FE gate
        # branches into "demo mode active" panel).
        r = await ac.get("/api/v1/auth/me")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "demo_fallback"
        assert body["email"] == "demo@example.com"
        # Demo mode bypasses the 401 gate on protected paths.
        r2 = await ac.get("/api/v1/repos")
        assert r2.status_code == 200


@pytest.mark.asyncio
async def test_logout_clears_principal_and_returns_post_state(
    reset_identity_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIFROST_DEMO_MODE", "true")
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/v1/auth/logout")
        assert r.status_code == 200
        body = r.json()
        # Demo mode → post-logout principal is the fresh demo principal.
        assert body["source"] == "demo_fallback"
