"""Tests for the bearer-token gate after the move to ``api_tokens``.

The router-level ``require_api_token`` gate is gone. Every ``/api/v1/*`` route (except health and auth) needs a principal
from IdentityMiddleware, and ``bft_`` API tokens are one principal source
(see tests/test_api_tokens.py for the token behaviour in full).
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from bifrost.cli.config import reset_settings

TOKEN = "bft_" + "a" * 43
DIGEST = hashlib.sha256(TOKEN.encode()).hexdigest()


@pytest.fixture(autouse=True)
def _reset_settings_each_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """The settings singleton caches env at construction; reset around
    each test so monkeypatch'd env vars actually take effect."""
    monkeypatch.delenv("BIFROST_API_TOKENS", raising=False)
    monkeypatch.setenv("BIFROST_DEMO_MODE", "false")
    reset_settings()
    yield
    reset_settings()


class TestApiRouterWiring:
    def test_api_router_has_no_router_level_gate(self) -> None:
        from bifrost.api.routes import api_router

        assert api_router.dependencies == []

    def test_public_router_has_no_gate(self) -> None:
        """Health stays open."""
        from bifrost.api.routes import public_router

        assert public_router.dependencies == []

    def test_legacy_gate_is_gone(self) -> None:
        from bifrost.api import deps

        assert not hasattr(deps, "require_api_token")


class TestEndToEndBehavior:
    """Black-box checks via TestClient (loopback peer, none provider)."""

    def _build_client(self, monkeypatch: pytest.MonkeyPatch, *, tokens: bool) -> TestClient:
        from bifrost.api.app import create_app

        if tokens:
            entry = [{"name": "smoke", "sha256": DIGEST, "roles": ["member"]}]
            monkeypatch.setenv("BIFROST_API_TOKENS", json.dumps(entry))
        monkeypatch.setenv("BIFROST_OPERATOR_EMAIL", "op@example.com")
        reset_settings()
        app = create_app()
        app.router.lifespan_context = None  # type: ignore[assignment]
        # TestClient's default peer is "testclient"; the none provider only
        # serves loopback, so present a loopback peer.
        return TestClient(app, client=("127.0.0.1", 50000))

    def test_health_is_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._build_client(monkeypatch, tokens=True)
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/health/detailed").status_code == 200

    def test_wrong_bearer_is_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._build_client(monkeypatch, tokens=True)
        resp = client.get("/api/v1/repos", headers={"Authorization": "Bearer wrong-token"})
        assert resp.status_code == 401
        assert resp.json()["code"] == "invalid_credentials"

    def test_valid_token_resolves_a_token_principal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._build_client(monkeypatch, tokens=True)
        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {TOKEN}"})
        assert resp.status_code == 200
        assert resp.json()["email"] == "token:smoke"
