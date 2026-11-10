"""Tests for production-mode startup validation.

The validator in ``BifrostSettings.validate_production_secrets()`` is
called at lifespan startup. We don't spin up the full app here — the
contract is a pure function over settings, so unit-testing the method
directly is cleaner and faster than booting the app.
"""

from __future__ import annotations

import os

import pytest

from bifrost.cli.config import BifrostSettings

HEADER_IDENTITY: dict[str, object] = {
    "provider": "header",
    "trusted_proxy": True,
    "roles": {"admin": ["eng"]},
}
OIDC_IDENTITY: dict[str, object] = {
    "provider": "oidc_jwt",
    "roles": {"admin": ["eng"]},
    "oidc": {
        "jwks_url": "https://idp.example.com/oauth2/default/v1/keys",
        "issuer": "https://idp.example.com/oauth2/default",
        "audience": "bifrost",
    },
}


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove any BIFROST_* env vars so kwargs-only settings are deterministic.

    The session-scoped conftest sets BIFROST_DEMO_MODE=true to keep
    IdentityMiddleware happy in API-route tests. Pydantic
    Settings gives keyword-args precedence over env vars, but only if
    the env var is unset OR matches — to make these unit tests
    purely declarative, scrub every BIFROST_* var on the way in.
    """
    for key in [k for k in os.environ if k.startswith("BIFROST_")]:
        monkeypatch.delenv(key, raising=False)


def _settings(**overrides: object) -> BifrostSettings:
    """Construct a BifrostSettings without reading the environment.

    Pydantic v2 reads env at instantiation time, so passing keyword
    arguments bypasses the env entirely — exactly what we want for
    deterministic tests.
    """
    base: dict[str, object] = {
        "_env_file": None,  # don't read .env from cwd
    }
    base.update(overrides)
    return BifrostSettings(**base)  # type: ignore[arg-type]


class TestProductionModeOff:
    def test_no_errors_when_production_mode_off(self) -> None:
        """The validator is a no-op when production_mode is False."""
        errors = _settings(production_mode=False).validate_production_secrets()
        assert errors == []

    def test_no_errors_even_with_dev_defaults(self) -> None:
        """Dev defaults are fine when production_mode is False."""
        s = _settings(
            production_mode=False,
            session_signing_key=None,
            cors_origins="http://localhost:5173,http://127.0.0.1:5173",
        )
        assert s.validate_production_secrets() == []


class TestProductionModeOn:
    def test_missing_session_signing_key_errors(self) -> None:
        errors = _settings(
            production_mode=True,
            session_signing_key=None,
            database_url="postgresql+asyncpg://prod-db:5432/bifrost",
            cors_origins="https://bifrost.example.com",
            identity=HEADER_IDENTITY,
        ).validate_production_secrets()
        assert any("SESSION_SIGNING_KEY" in e for e in errors)

    def test_dev_database_url_errors(self) -> None:
        errors = _settings(
            production_mode=True,
            session_signing_key="some-key",
            cors_origins="https://bifrost.example.com",
            identity=HEADER_IDENTITY,
        ).validate_production_secrets()
        assert any("DATABASE_URL" in e for e in errors)

    def test_dev_cors_origins_errors(self) -> None:
        errors = _settings(
            production_mode=True,
            session_signing_key="some-key",
            database_url="postgresql+asyncpg://prod-db:5432/bifrost",
            identity=HEADER_IDENTITY,
        ).validate_production_secrets()
        assert any("CORS_ORIGINS" in e for e in errors)

    def test_no_identity_mode_errors(self) -> None:
        """Either service-mesh trust or local JWT verification must be on."""
        errors = _settings(
            production_mode=True,
            session_signing_key="some-key",
            database_url="postgresql+asyncpg://prod-db:5432/bifrost",
            cors_origins="https://bifrost.example.com",
        ).validate_production_secrets()
        assert any("identity.provider" in e for e in errors)
        assert not any("alias" in e.lower() for e in errors)

    def test_jwt_verify_without_jwk_url_errors(self) -> None:
        errors = _settings(
            production_mode=True,
            session_signing_key="some-key",
            database_url="postgresql+asyncpg://prod-db:5432/bifrost",
            cors_origins="https://bifrost.example.com",
            identity={"provider": "oidc_jwt"},
        ).validate_production_secrets()
        assert any("identity.oidc.jwks_url" in e for e in errors)
        assert not any("alias" in e.lower() for e in errors)

    def test_demo_mode_incompatible_with_production(self) -> None:
        errors = _settings(
            production_mode=True,
            session_signing_key="some-key",
            database_url="postgresql+asyncpg://prod-db:5432/bifrost",
            cors_origins="https://bifrost.example.com",
            identity=HEADER_IDENTITY,
            demo_mode=True,
        ).validate_production_secrets()
        assert any("DEMO_MODE" in e for e in errors)

    def test_fully_configured_passes(self) -> None:
        errors = _settings(
            production_mode=True,
            credential_key="YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
            session_signing_key="32-char-secure-key-from-token-urlsafe",
            database_url="postgresql+asyncpg://prod-db.example.com:5432/bifrost",
            cors_origins="https://bifrost.example.com",
            identity=HEADER_IDENTITY,
            demo_mode=False,
        ).validate_production_secrets()
        assert errors == []

    def test_jwt_verifier_path_passes(self) -> None:
        """Production with Bifrost-side JWT verification (no service mesh)."""
        errors = _settings(
            production_mode=True,
            credential_key="YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
            session_signing_key="32-char-secure-key-from-token-urlsafe",
            database_url="postgresql+asyncpg://prod-db.example.com:5432/bifrost",
            cors_origins="https://bifrost.example.com",
            identity=OIDC_IDENTITY,
        ).validate_production_secrets()
        assert errors == []


class TestErrorMessagesAreHelpful:
    """Error strings should tell the operator how to fix the misconfig."""

    def test_session_key_error_includes_generation_recipe(self) -> None:
        errors = _settings(
            production_mode=True,
            session_signing_key=None,
            database_url="postgresql+asyncpg://prod-db:5432/bifrost",
            cors_origins="https://bifrost.example.com",
            identity=HEADER_IDENTITY,
        ).validate_production_secrets()
        signing_err = next((e for e in errors if "SESSION_SIGNING_KEY" in e), "")
        assert "secrets.token_urlsafe" in signing_err

    @pytest.mark.parametrize(
        ("field", "value", "expect_substr"),
        [
            ("database_url", None, "DATABASE_URL"),  # default catches it
            ("cors_origins", None, "CORS_ORIGINS"),
        ],
    )
    def test_default_value_is_flagged(
        self,
        field: str,
        value: object,
        expect_substr: str,
    ) -> None:
        kwargs: dict[str, object] = {
            "production_mode": True,
            "session_signing_key": "some-key",
            "identity": HEADER_IDENTITY,
        }
        # Either explicitly pass the dev default OR rely on the
        # field default by not passing it at all.
        if value is not None:
            kwargs[field] = value
        # Set the OTHER required field so the only error is the one
        # we're testing.
        if field != "database_url":
            kwargs["database_url"] = "postgresql+asyncpg://prod-db:5432/bifrost"
        if field != "cors_origins":
            kwargs["cors_origins"] = "https://bifrost.example.com"
        errors = _settings(**kwargs).validate_production_secrets()
        assert any(expect_substr in e for e in errors), errors
