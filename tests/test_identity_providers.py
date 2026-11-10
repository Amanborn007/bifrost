"""Identity providers: none / header / oidc_jwt."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from bifrost.api.auth.principal import (
    SOURCE_DEMO_FALLBACK,
    SOURCE_ENV_OVERRIDE,
    SOURCE_OIDC_JWT,
    SOURCE_SSO_HEADER,
)
from bifrost.api.auth.providers import (
    IdentityConfigError,
    InvalidCredentials,
    build_identity_provider,
)
from bifrost.cli.config import BifrostSettings, reset_settings

ISSUER = "https://idp.example.com/"
AUDIENCE = "bifrost"
JWKS_URL = "https://idp.example.com/.well-known/jwks.json"
SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "bifrost"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in (
        "BIFROST_DEMO_MODE",
        "BIFROST_OPERATOR_EMAIL",
        "BIFROST_OPERATOR_GROUPS",
    ):
        monkeypatch.delenv(key, raising=False)
    reset_settings()
    yield
    reset_settings()


def _settings(**overrides: Any) -> BifrostSettings:
    return BifrostSettings(_env_file=None, **overrides)  # type: ignore[call-arg]


def _request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": "GET", "path": "/", "headers": raw}
    return Request({**scope, "client": ("127.0.0.1", 50000)})


# ── none ────────────────────────────────────────────────────────────


async def test_none_provider_resolves_operator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIFROST_OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setenv("BIFROST_OPERATOR_GROUPS", "bifrost-admin, bifrost-engineer")
    provider = build_identity_provider(_settings())
    principal = await provider.resolve(_request())
    assert principal is not None
    assert principal.email == "op@example.com"
    assert principal.source == SOURCE_ENV_OVERRIDE
    assert principal.groups == ("bifrost-admin", "bifrost-engineer")


async def test_none_provider_demo_mode_uses_configured_demo_email() -> None:
    provider = build_identity_provider(_settings(demo_mode=True))
    principal = await provider.resolve(_request())
    assert principal is not None
    assert principal.email == "demo@example.com"
    assert principal.source == SOURCE_DEMO_FALLBACK


async def test_none_provider_demo_email_is_configurable() -> None:
    settings = _settings(demo_mode=True, identity={"demo_email": "visitor@corp.test"})
    principal = await build_identity_provider(settings).resolve(_request())
    assert principal is not None
    assert principal.email == "visitor@corp.test"


async def test_none_provider_without_env_or_demo_resolves_nothing() -> None:
    assert await build_identity_provider(_settings()).resolve(_request()) is None


async def test_none_provider_ignores_proxy_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = build_identity_provider(_settings())
    spoofed = _request({"X-Forwarded-Email": "spoof@evil.test"})
    assert await provider.resolve(spoofed) is None


def test_none_provider_reports_no_interactive_refresh() -> None:
    provider = build_identity_provider(_settings())
    assert provider.name == "none"
    assert provider.interactive_refresh is False


# ── header ──────────────────────────────────────────────────────────


def _header_settings(**identity: Any) -> BifrostSettings:
    return _settings(
        identity={"provider": "header", "trusted_proxy": True, "roles": {"admin": []}, **identity}
    )


async def test_header_provider_resolves_default_header_names() -> None:
    provider = build_identity_provider(_header_settings())
    principal = await provider.resolve(
        _request({"X-Forwarded-Email": "a@example.com", "X-Forwarded-Groups": "eng, ops,eng"})
    )
    assert principal is not None
    assert principal.email == "a@example.com"
    assert principal.groups == ("eng", "ops")
    assert principal.source == SOURCE_SSO_HEADER


async def test_header_provider_honours_configured_header_names() -> None:
    provider = build_identity_provider(
        _header_settings(header={"email": "X-Auth-Request-Email", "groups": "X-Auth-Groups"})
    )
    principal = await provider.resolve(
        _request({"X-Auth-Request-Email": "b@example.com", "X-Auth-Groups": "ops"})
    )
    assert principal is not None
    assert (principal.email, principal.groups) == ("b@example.com", ("ops",))
    assert await provider.resolve(_request({"X-Forwarded-Email": "b@example.com"})) is None


async def test_header_provider_without_header_resolves_nothing() -> None:
    assert await build_identity_provider(_header_settings()).resolve(_request()) is None


def test_header_mode_without_trusted_proxy_fails_to_build() -> None:
    with pytest.raises(IdentityConfigError, match="trusted_proxy"):
        build_identity_provider(_settings(identity={"provider": "header"}))


def test_header_mode_without_trusted_proxy_fails_app_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIFROST_IDENTITY__PROVIDER", "header")
    reset_settings()
    from bifrost.api.app import create_app

    with pytest.raises(IdentityConfigError, match="trusted_proxy"):
        create_app()


def test_header_mode_rejects_demo_mode() -> None:
    with pytest.raises(IdentityConfigError, match="demo"):
        build_identity_provider(
            _settings(demo_mode=True, identity={"provider": "header", "trusted_proxy": True})
        )


async def _client(app: object) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")  # type: ignore[arg-type]


async def test_forwarded_headers_are_ignored_unless_provider_is_header() -> None:
    reset_settings()
    from bifrost.api.app import create_app

    app = create_app()
    async with await _client(app) as ac:
        r = await ac.get("/api/v1/repos", headers={"X-Forwarded-Email": "spoof@evil.test"})
        assert r.status_code == 401
        me = await ac.get("/api/v1/auth/me", headers={"X-Forwarded-Email": "spoof@evil.test"})
        assert me.status_code == 401


async def test_forwarded_headers_do_not_override_operator_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIFROST_OPERATOR_EMAIL", "op@example.com")
    reset_settings()
    from bifrost.api.app import create_app

    async with await _client(create_app()) as ac:
        me = await ac.get("/api/v1/auth/me", headers={"X-Forwarded-Email": "spoof@evil.test"})
        assert me.json()["email"] == "op@example.com"


async def test_header_provider_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIFROST_IDENTITY__PROVIDER", "header")
    monkeypatch.setenv("BIFROST_IDENTITY__TRUSTED_PROXY", "true")
    monkeypatch.setenv("BIFROST_IDENTITY__ROLES", '{"admin": []}')
    reset_settings()
    from bifrost.api.app import create_app

    async with await _client(create_app()) as ac:
        me = await ac.get("/api/v1/auth/me", headers={"X-Forwarded-Email": "real@example.com"})
        assert me.status_code == 200
        body = me.json()
        assert body["email"] == "real@example.com"
        assert body["source"] == SOURCE_SSO_HEADER
        assert body["provider"] == "header"
        assert body["capabilities"]["interactive_refresh"] is False
        assert (await ac.get("/api/v1/auth/me")).status_code == 401


# ── oidc_jwt ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def signing_key() -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks(monkeypatch: pytest.MonkeyPatch, signing_key: Any) -> None:
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    jwk.update({"kid": "test-kid", "use": "sig", "alg": "RS256"})
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: {"keys": [jwk]})


def _token(key: Any, **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 600,
        "email": "jwt@example.com",
        "groups": ["eng", "ops"],
    }
    claims.update({k: v for k, v in overrides.items() if v is not None})
    for k, v in overrides.items():
        if v is None:
            claims.pop(k, None)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-kid"})


def _oidc_settings(**identity: Any) -> BifrostSettings:
    oidc = {"jwks_url": JWKS_URL, "issuer": ISSUER, "audience": AUDIENCE}
    return _settings(
        identity={"provider": "oidc_jwt", "oidc": oidc, "roles": {"admin": []}, **identity}
    )


def _bearer(token: str) -> Request:
    return _request({"Authorization": f"Bearer {token}"})


async def test_oidc_resolves_valid_token(jwks: None, signing_key: Any) -> None:
    provider = build_identity_provider(_oidc_settings())
    principal = await provider.resolve(_bearer(_token(signing_key)))
    assert principal is not None
    assert principal.email == "jwt@example.com"
    assert principal.groups == ("eng", "ops")
    assert principal.source == SOURCE_OIDC_JWT


async def test_oidc_falls_back_to_sub_when_it_is_an_email(jwks: None, signing_key: Any) -> None:
    provider = build_identity_provider(_oidc_settings())
    token = _token(signing_key, email=None, sub="sub@example.com")
    principal = await provider.resolve(_bearer(token))
    assert principal is not None
    assert principal.email == "sub@example.com"


async def test_oidc_without_bearer_resolves_nothing(jwks: None) -> None:
    provider = build_identity_provider(_oidc_settings())
    assert await provider.resolve(_request()) is None
    assert await provider.resolve(_request({"Authorization": "Basic abc"})) is None


@pytest.mark.parametrize(
    "bad",
    ["signature", "issuer", "audience", "expired", "no_email", "garbage"],
)
async def test_oidc_rejects_bad_tokens(jwks: None, signing_key: Any, bad: str) -> None:
    provider = build_identity_provider(_oidc_settings())
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    tokens = {
        "signature": _token(other_key),
        "issuer": _token(signing_key, iss="https://evil.example.com/"),
        "audience": _token(signing_key, aud="someone-else"),
        "expired": _token(signing_key, iat=now - 7200, exp=now - 3600),
        "no_email": _token(signing_key, email=None, sub="uuid-without-at"),
        "garbage": "not.a.jwt",
    }
    with pytest.raises(InvalidCredentials):
        await provider.resolve(_bearer(tokens[bad]))


@pytest.mark.parametrize("missing", ["jwks_url", "issuer", "audience"])
def test_oidc_requires_jwks_issuer_and_audience(missing: str) -> None:
    oidc = {"jwks_url": JWKS_URL, "issuer": ISSUER, "audience": AUDIENCE}
    oidc.pop(missing)
    with pytest.raises(IdentityConfigError, match=missing):
        build_identity_provider(_settings(identity={"provider": "oidc_jwt", "oidc": oidc}))


async def test_oidc_end_to_end_rejects_bad_token_with_401(
    monkeypatch: pytest.MonkeyPatch, jwks: None, signing_key: Any
) -> None:
    monkeypatch.setenv("BIFROST_IDENTITY__PROVIDER", "oidc_jwt")
    monkeypatch.setenv("BIFROST_IDENTITY__ROLES", '{"admin": []}')
    monkeypatch.setenv("BIFROST_IDENTITY__OIDC__JWKS_URL", JWKS_URL)
    monkeypatch.setenv("BIFROST_IDENTITY__OIDC__ISSUER", ISSUER)
    monkeypatch.setenv("BIFROST_IDENTITY__OIDC__AUDIENCE", AUDIENCE)
    reset_settings()
    from bifrost.api.app import create_app

    async with await _client(create_app()) as ac:
        good = await ac.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {_token(signing_key)}"}
        )
        assert good.status_code == 200
        assert good.json()["provider"] == "oidc_jwt"
        bad = await ac.get(
            "/api/v1/repos", headers={"Authorization": f"Bearer {_token(signing_key, aud='x')}"}
        )
        assert bad.status_code == 401
        assert bad.json()["code"] == "invalid_credentials"
        none = await ac.get("/api/v1/repos")
        assert none.status_code == 401
        assert none.json()["code"] == "identity_required"
