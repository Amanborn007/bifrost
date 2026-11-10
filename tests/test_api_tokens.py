"""API tokens (``bft_``) as a principal source, and the legacy settings
they replace."""

from __future__ import annotations

import hashlib
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from typer.testing import CliRunner

import bifrost.api.audit as audit_mod
from bifrost.api.deps import get_repository
from bifrost.cli.config import reset_settings
from bifrost.errors import ConfigError
from tests.auth_helpers import (
    AUDIENCE,
    ISSUER,
    boot,
    client,
    header_identity,
    new_token,
    token_entry,
)

REPO_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _reset_settings():
    reset_settings()
    yield
    reset_settings()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_bft_token_resolves_a_token_principal(monkeypatch) -> None:
    token, _ = new_token()
    app = boot(monkeypatch, tokens=[token_entry("ci", token)])
    async with client(app) as ac:
        resp = await ac.get("/api/v1/auth/me", headers=_bearer(token))
    body = resp.json()
    assert resp.status_code == 200, resp.text
    assert body["email"] == "token:ci"
    assert body["source"] == "api_token"
    assert body["roles"] == ["member"]


async def test_token_roles_use_the_same_vocabulary(monkeypatch) -> None:
    token, _ = new_token()
    app = boot(monkeypatch, tokens=[token_entry("deploy", token, ["admin"])])
    async with client(app) as ac:
        body = (await ac.get("/api/v1/auth/me", headers=_bearer(token))).json()
    assert set(body["roles"]) == {"member", "admin"}


async def test_two_tokens_are_both_valid_for_rotation(monkeypatch) -> None:
    old, _ = new_token()
    new, _ = new_token()
    app = boot(monkeypatch, tokens=[token_entry("ci-old", old), token_entry("ci-new", new)])
    async with client(app) as ac:
        a = await ac.get("/api/v1/auth/me", headers=_bearer(old))
        b = await ac.get("/api/v1/auth/me", headers=_bearer(new))
    assert (a.json()["email"], b.json()["email"]) == ("token:ci-old", "token:ci-new")


async def test_wrong_bft_token_is_401_invalid_credentials(monkeypatch) -> None:
    token, _ = new_token()
    wrong, _ = new_token()
    app = boot(monkeypatch, tokens=[token_entry("ci", token)])
    async with client(app) as ac:
        resp = await ac.get("/api/v1/repos", headers=_bearer(wrong))
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"


async def test_bft_token_with_no_tokens_configured_is_401(monkeypatch) -> None:
    token, _ = new_token()
    app = boot(monkeypatch)
    async with client(app) as ac:
        resp = await ac.get("/api/v1/repos", headers=_bearer(token))
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"


async def test_token_compares_against_every_digest(monkeypatch) -> None:
    first, _ = new_token()
    second, _ = new_token()
    third, _ = new_token()
    app = boot(
        monkeypatch,
        tokens=[token_entry("a", first), token_entry("b", second), token_entry("c", third)],
    )
    from bifrost.api.auth import tokens as tokens_mod

    calls: list[tuple[str, str]] = []
    real = tokens_mod.hmac.compare_digest

    def _spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(tokens_mod.hmac, "compare_digest", _spy)
    async with client(app) as ac:
        resp = await ac.get("/api/v1/auth/me", headers=_bearer(first))
    assert resp.json()["email"] == "token:a"
    assert len(calls) == 3
    assert {b for _, b in calls} == {
        hashlib.sha256(t.encode()).hexdigest() for t in (first, second, third)
    }


@pytest.mark.parametrize("provider", ["none", "header"])
async def test_non_bft_bearer_without_oidc_is_401_invalid_credentials(
    monkeypatch, provider: str
) -> None:
    app = boot(
        monkeypatch, provider=provider, roles={"admin": []} if provider == "header" else None
    )
    headers = {**_bearer("some-static-secret"), **header_identity("u@example.com")}
    async with client(app) as ac:
        resp = await ac.get("/api/v1/repos", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"


@pytest.fixture(scope="module")
def signing_key() -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks(monkeypatch, signing_key) -> None:
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    jwk.update({"kid": "test-kid", "use": "sig", "alg": "RS256"})
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: {"keys": [jwk]})


def _jwt(key: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 600,
        "email": "j@example.com",
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-kid"})


async def test_tokens_and_oidc_boot_together_and_dispatch_on_prefix(
    monkeypatch, jwks, signing_key
) -> None:
    token, _ = new_token()
    app = boot(
        monkeypatch, provider="oidc_jwt", roles={"admin": []}, tokens=[token_entry("ci", token)]
    )
    async with client(app) as ac:
        via_token = await ac.get("/api/v1/auth/me", headers=_bearer(token))
        via_jwt = await ac.get("/api/v1/auth/me", headers=_bearer(_jwt(signing_key)))
        bad_jwt = await ac.get("/api/v1/repos", headers=_bearer("not.a.jwt"))
    assert via_token.json()["source"] == "api_token"
    assert via_jwt.json()["source"] == "oidc_jwt"
    assert bad_jwt.status_code == 401
    assert bad_jwt.json()["code"] == "invalid_credentials"


# ── gating ──────────────────────────────────────────────────────────


@pytest.fixture
def audit_events(monkeypatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> None:
        events.append(kwargs)

    monkeypatch.setattr(audit_mod, "record_event", _record)
    return events


def _stub_repo(app) -> None:
    async def _repo():
        mock = MagicMock()
        mock.get_repo = AsyncMock(return_value=MagicMock(id=REPO_ID))
        yield mock

    app.dependency_overrides[get_repository] = _repo


async def test_member_token_is_refused_and_admin_token_allowed(monkeypatch, audit_events) -> None:
    member, _ = new_token()
    admin, _ = new_token()
    app = boot(
        monkeypatch,
        tokens=[token_entry("reader", member), token_entry("deploy", admin, ["admin"])],
    )
    _stub_repo(app)
    async with client(app) as ac:
        denied = await ac.delete(f"/api/v1/repos/{REPO_ID}", headers=_bearer(member))
        allowed = await ac.delete(f"/api/v1/repos/{REPO_ID}", headers=_bearer(admin))
    assert denied.status_code == 403
    assert allowed.status_code == 200
    granted = [e for e in audit_events if e["event_type"] == "admin.access.granted"]
    assert [e["actor"] for e in granted] == ["token:deploy"]


async def test_token_works_from_a_non_loopback_client_under_none_provider(monkeypatch) -> None:
    token, _ = new_token()
    app = boot(monkeypatch, tokens=[token_entry("ci", token)])
    async with client(app, host="203.0.113.7") as ac:
        resp = await ac.get("/api/v1/auth/me", headers=_bearer(token))
        anon = await ac.get("/api/v1/auth/me")
    assert resp.status_code == 200
    assert anon.status_code == 401


# ── config validation ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "ci", "sha256": "not-hex", "roles": ["member"]},
        {"name": "ci", "sha256": "0" * 64, "roles": ["root"]},
        {"name": "bad name!", "sha256": "0" * 64, "roles": ["member"]},
    ],
)
def test_malformed_token_entries_fail_startup(monkeypatch, entry: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        boot(monkeypatch, tokens=[entry])


def test_duplicate_token_names_fail_startup(monkeypatch) -> None:
    entry = {"name": "ci", "sha256": "0" * 64, "roles": ["member"]}
    with pytest.raises(ConfigError, match="ci"):
        boot(monkeypatch, tokens=[entry, {**entry, "sha256": "1" * 64}])


def test_router_level_api_token_gate_is_gone() -> None:
    from bifrost.api import deps
    from bifrost.api.routes import api_router

    assert not hasattr(deps, "require_api_token")
    assert not hasattr(deps, "require_admin_token")
    assert api_router.dependencies == []


# ── CLI ─────────────────────────────────────────────────────────────


def test_token_new_prints_token_once_with_its_sha256() -> None:
    from bifrost.cli.app import app as cli_app

    runner = CliRunner()
    first = runner.invoke(cli_app, ["token", "new"])
    second = runner.invoke(cli_app, ["token", "new"])
    assert first.exit_code == 0, first.output
    token = next(w for w in first.output.split() if w.startswith("bft_"))
    assert hashlib.sha256(token.encode()).hexdigest() in first.output
    assert first.output.count(token) == 1
    other = next(w for w in second.output.split() if w.startswith("bft_"))
    assert other != token


def test_token_new_prints_a_config_snippet() -> None:
    from bifrost.cli.app import app as cli_app

    out = CliRunner().invoke(cli_app, ["token", "new", "--name", "ci", "--role", "admin"]).output
    token = next(w for w in out.split() if w.startswith("bft_"))
    digest = hashlib.sha256(token.encode()).hexdigest()
    assert "name: ci" in out and f"sha256: {digest}" in out and "admin" in out


def test_cli_client_sends_bifrost_token_when_set(monkeypatch) -> None:
    from bifrost.cli.commands._http import get_client

    monkeypatch.setenv("BIFROST_TOKEN", "bft_abc")
    reset_settings()
    with get_client() as c:
        assert c.headers["Authorization"] == "Bearer bft_abc"


def test_cli_client_sends_no_authorization_without_token(monkeypatch) -> None:
    from bifrost.cli.commands._http import get_client

    monkeypatch.delenv("BIFROST_TOKEN", raising=False)
    reset_settings()
    with get_client() as c:
        assert "Authorization" not in c.headers
