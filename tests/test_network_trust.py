"""Network trust: the ``none`` provider only serves loopback clients, and
the ``header`` provider only believes identity headers from trusted peers."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from bifrost.api.auth.providers import IdentityConfigError
from bifrost.cli.config import reset_settings
from bifrost.errors import ConfigError
from tests.auth_helpers import boot, client, header_identity

ROLES = {"admin": ["eng-admins"]}


@pytest.fixture(autouse=True)
def _reset_settings():
    reset_settings()
    yield
    reset_settings()


# ── none provider ───────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.9.10", "::1"])
async def test_none_provider_serves_loopback_clients(monkeypatch, host: str) -> None:
    app = boot(monkeypatch, provider="none", operator_email="op@example.com")
    async with client(app, host=host) as ac:
        resp = await ac.get("/api/v1/auth/me")
    assert resp.status_code == 200
    assert resp.json()["email"] == "op@example.com"


@pytest.mark.parametrize("host", ["203.0.113.9", "10.0.0.5", "2001:db8::1", "testclient"])
@pytest.mark.parametrize("mode", ["operator", "demo"])
async def test_none_provider_refuses_non_loopback_clients(
    monkeypatch, host: str, mode: str
) -> None:
    app = boot(
        monkeypatch,
        provider="none",
        operator_email="op@example.com" if mode == "operator" else None,
        demo=mode == "demo",
    )
    async with client(app, host=host) as ac:
        protected = await ac.get("/api/v1/repos")
        me = await ac.get("/api/v1/auth/me")
    assert protected.status_code == 401
    assert protected.json()["code"] == "identity_required"
    assert me.status_code == 401


async def test_none_provider_ignores_forwarded_for_spoofing(monkeypatch) -> None:
    app = boot(monkeypatch, provider="none", operator_email="op@example.com")
    async with client(app, host="203.0.113.9") as ac:
        resp = await ac.get("/api/v1/repos", headers={"X-Forwarded-For": "127.0.0.1"})
    assert resp.status_code == 401


# ── header provider ─────────────────────────────────────────────────


async def test_header_identity_from_loopback_peer_is_trusted_by_default(monkeypatch) -> None:
    app = boot(monkeypatch, provider="header", roles=ROLES)
    async with client(app, host="127.0.0.1") as ac:
        resp = await ac.get("/api/v1/auth/me", headers=header_identity("a@example.com"))
    assert resp.status_code == 200


@pytest.mark.parametrize("host", ["203.0.113.9", "10.1.2.3", "2001:db8::1", "testclient"])
async def test_header_identity_from_untrusted_peer_is_ignored(monkeypatch, host: str) -> None:
    app = boot(monkeypatch, provider="header", roles=ROLES)
    headers = header_identity("evil@example.com", "eng-admins")
    async with client(app, host=host) as ac:
        me = await ac.get("/api/v1/auth/me", headers=headers)
        protected = await ac.get("/api/v1/repos", headers=headers)
    assert me.status_code == 401
    assert protected.status_code == 401
    assert protected.json()["code"] == "identity_required"


async def test_header_identity_from_configured_cidr_is_trusted(monkeypatch) -> None:
    app = boot(monkeypatch, provider="header", roles=ROLES, cidrs=["10.0.0.0/8"])
    async with client(app, host="10.1.2.3") as ac:
        inside = await ac.get("/api/v1/auth/me", headers=header_identity("a@example.com"))
    async with client(app, host="127.0.0.1") as ac:
        loopback = await ac.get("/api/v1/auth/me", headers=header_identity("a@example.com"))
    assert inside.status_code == 200
    assert loopback.status_code == 401


async def test_untrusted_peer_headers_are_stripped_before_handlers_run(monkeypatch) -> None:
    app = boot(monkeypatch, provider="header", roles=ROLES)

    @app.get("/api/v1/health/echo-identity")
    async def _echo(request: Request) -> dict[str, str | None]:
        return {
            "email": request.headers.get("x-forwarded-email"),
            "groups": request.headers.get("x-forwarded-groups"),
        }

    async with client(app, host="203.0.113.9") as ac:
        resp = await ac.get(
            "/api/v1/health/echo-identity", headers=header_identity("evil@example.com", "x")
        )
    assert resp.json() == {"email": None, "groups": None}


@pytest.mark.parametrize("bad", [["not-a-cidr"], ["10.0.0.0/33"], []])
def test_invalid_trusted_proxy_cidrs_fail_startup(monkeypatch, bad: list[str]) -> None:
    with pytest.raises((ConfigError, IdentityConfigError)):
        boot(monkeypatch, provider="header", roles=ROLES, cidrs=bad)
