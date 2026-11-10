"""Roles: IdP groups map to ``member`` / ``admin`` once, in the middleware."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import bifrost.api.audit as audit_mod
from bifrost.api.auth.providers import IdentityConfigError, build_identity_provider
from bifrost.api.deps import get_repository
from bifrost.cli.config import BifrostSettings, reset_settings
from bifrost.errors import ConfigError
from tests.auth_helpers import AUDIENCE, ISSUER, JWKS_URL, boot, client, header_identity

ADMIN_MAP = {"admin": ["eng-admins"]}
REPO_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _reset_settings():
    reset_settings()
    yield
    reset_settings()


async def _me(app, headers: dict[str, str]) -> dict[str, Any]:
    async with client(app) as ac:
        resp = await ac.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_admin_group_maps_to_admin_role(monkeypatch) -> None:
    app = boot(monkeypatch, provider="header", roles=ADMIN_MAP)
    body = await _me(app, header_identity("a@example.com", "eng, eng-admins"))
    assert set(body["roles"]) == {"member", "admin"}


async def test_unmapped_user_is_member(monkeypatch) -> None:
    app = boot(monkeypatch, provider="header", roles=ADMIN_MAP)
    body = await _me(app, header_identity("m@example.com", "eng"))
    assert body["roles"] == ["member"]


async def test_raw_bifrost_admin_group_is_not_admin(monkeypatch) -> None:
    app = boot(monkeypatch, provider="header", roles=ADMIN_MAP)
    body = await _me(app, header_identity("m@example.com", "bifrost-admin"))
    assert body["roles"] == ["member"]
    assert "bifrost-admin" in body["groups"]


async def test_empty_admin_mapping_boots_and_everyone_is_member(monkeypatch) -> None:
    app = boot(monkeypatch, provider="header", roles={"admin": []})
    body = await _me(app, header_identity("m@example.com", "eng-admins,bifrost-admin"))
    assert body["roles"] == ["member"]


async def test_header_provider_reads_configured_groups_header(monkeypatch) -> None:
    app = boot(monkeypatch, provider="header", roles=ADMIN_MAP, groups_header="X-Auth-Groups")
    headers = {"X-Forwarded-Email": "a@example.com", "X-Auth-Groups": "eng-admins"}
    assert "admin" in (await _me(app, headers))["roles"]
    ignored = header_identity("a@example.com", "eng-admins")
    assert (await _me(app, ignored))["roles"] == ["member"]


async def test_none_provider_operator_is_admin(monkeypatch) -> None:
    app = boot(monkeypatch, provider="none", operator_email="op@example.com")
    assert set((await _me(app, {}))["roles"]) == {"member", "admin"}


async def test_demo_principal_is_admin_and_has_no_hardcoded_admin_group(monkeypatch) -> None:
    app = boot(monkeypatch, provider="none", demo=True)
    body = await _me(app, {})
    assert "admin" in body["roles"]
    assert "bifrost-admin" not in body["groups"]


@pytest.mark.parametrize("provider", ["header", "oidc_jwt"])
def test_roles_unset_fails_build(provider: str) -> None:
    identity: dict[str, Any] = {"provider": provider, "trusted_proxy": True}
    if provider == "oidc_jwt":
        identity["oidc"] = {"jwks_url": JWKS_URL, "issuer": ISSUER, "audience": AUDIENCE}
    settings = BifrostSettings(  # type: ignore[call-arg]
        _env_file=None, demo_mode=False, identity=identity
    )
    with pytest.raises(IdentityConfigError, match="identity.roles"):
        build_identity_provider(settings)


@pytest.mark.parametrize("provider", ["header", "oidc_jwt"])
def test_roles_unset_fails_app_startup(monkeypatch, provider: str) -> None:
    with pytest.raises(IdentityConfigError, match="identity.roles"):
        boot(monkeypatch, provider=provider, roles=None)


def test_unknown_role_name_in_mapping_is_rejected(monkeypatch) -> None:
    with pytest.raises(ConfigError, match="superuser"):
        boot(monkeypatch, provider="header", roles={"superuser": ["x"]})  # type: ignore[dict-item]


# ── oidc groups claim ───────────────────────────────────────────────


@pytest.fixture(scope="module")
def signing_key() -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks(monkeypatch, signing_key) -> None:
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    jwk.update({"kid": "test-kid", "use": "sig", "alg": "RS256"})
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: {"keys": [jwk]})


def _jwt(key: Any, **claims: Any) -> str:
    now = int(time.time())
    base = {"iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 600, "email": "j@example.com"}
    return jwt.encode({**base, **claims}, key, algorithm="RS256", headers={"kid": "test-kid"})


async def test_oidc_groups_claim_maps_to_roles(monkeypatch, jwks, signing_key) -> None:
    app = boot(
        monkeypatch,
        provider="oidc_jwt",
        roles=ADMIN_MAP,
        extra_env={"BIFROST_IDENTITY__OIDC__GROUPS_CLAIM": "idp:groups"},
    )
    admin = {"Authorization": f"Bearer {_jwt(signing_key, **{'idp:groups': ['eng-admins']})}"}
    other = {"Authorization": f"Bearer {_jwt(signing_key, **{'idp:groups': ['eng']})}"}
    assert "admin" in (await _me(app, admin))["roles"]
    assert (await _me(app, other))["roles"] == ["member"]


# ── route gating ────────────────────────────────────────────────────


@pytest.fixture
def audit_events(monkeypatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> None:
        events.append(kwargs)

    monkeypatch.setattr(audit_mod, "record_event", _record)
    return events


def _stub_repo_dependency(app) -> None:
    async def _repo():
        mock = MagicMock()
        mock.get_repo = AsyncMock(return_value=MagicMock(id=REPO_ID))
        yield mock

    app.dependency_overrides[get_repository] = _repo


async def test_member_delete_repo_is_403_with_stable_code(monkeypatch, audit_events) -> None:
    app = boot(monkeypatch, provider="header", roles=ADMIN_MAP)
    _stub_repo_dependency(app)
    async with client(app) as ac:
        resp = await ac.delete(
            f"/api/v1/repos/{REPO_ID}", headers=header_identity("m@example.com", "eng")
        )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "auth.role_required"
    denied = [e for e in audit_events if e["event_type"] == "admin.access.denied"]
    assert [e["actor"] for e in denied] == ["m@example.com"]


async def test_admin_delete_repo_succeeds_and_audit_records_actor(
    monkeypatch, audit_events
) -> None:
    app = boot(monkeypatch, provider="header", roles=ADMIN_MAP)
    _stub_repo_dependency(app)
    async with client(app) as ac:
        resp = await ac.delete(
            f"/api/v1/repos/{REPO_ID}", headers=header_identity("a@example.com", "eng-admins")
        )
    assert resp.status_code == 200, resp.text
    granted = [e for e in audit_events if e["event_type"] == "admin.access.granted"]
    assert len(granted) == 1
    assert granted[0]["actor"] == "a@example.com"
    assert granted[0]["method"] == "DELETE"
    assert granted[0]["resource"] == f"/api/v1/repos/{REPO_ID}"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/repos"),
        ("POST", "/api/v1/extract"),
        ("POST", "/api/v1/domains"),
        ("POST", "/api/v1/tasks/t1/execute"),
        ("POST", "/api/v1/tasks/t1/create-pr"),
        ("POST", "/api/v1/handover-docs/bulk/refresh-stale"),
        ("POST", "/api/v1/admin/embed/entities"),
        ("GET", "/api/v1/admin/audit"),
        ("GET", "/api/v1/admin/learning/stats"),
    ],
)
async def test_member_is_refused_on_admin_routes(
    monkeypatch, audit_events, method: str, path: str
) -> None:
    app = boot(monkeypatch, provider="header", roles=ADMIN_MAP)
    async with client(app) as ac:
        resp = await ac.request(
            method,
            path,
            json={} if method == "POST" else None,
            headers=header_identity("m@example.com", "eng"),
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "auth.role_required"


async def test_member_can_still_post_a_member_action(monkeypatch, audit_events) -> None:
    app = boot(monkeypatch, provider="header", roles=ADMIN_MAP)
    async with client(app) as ac:
        resp = await ac.post(
            "/api/v1/feedback", json={}, headers=header_identity("m@example.com", "eng")
        )
    assert resp.status_code == 422


async def test_member_cannot_create_jira_ticket_through_spawn_task(
    monkeypatch, audit_events
) -> None:
    app = boot(monkeypatch, provider="header", roles=ADMIN_MAP)
    body = {"conversation_id": REPO_ID, "create_jira_now": True, "project_key": "ABC"}
    async with client(app) as ac:
        resp = await ac.post(
            f"/api/v1/conversations/{REPO_ID}/spawn-task",
            json=body,
            headers=header_identity("m@example.com", "eng"),
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "auth.role_required"
