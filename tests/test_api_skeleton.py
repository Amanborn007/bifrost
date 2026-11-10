"""Tests for the API skeleton — health, CORS.

Stub route tests removed in S12 — routes now return real responses.
See test_api_routes.py for full route coverage.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bifrost.api.app import create_app


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    return TestClient(app)


class TestHealth:
    def test_health_returns_200(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_body(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.json() == {"status": "ok"}


class TestCORS:
    def test_cors_allows_listed_origin(self, client: TestClient) -> None:
        # The allowlist defaults to the
        # Vite dev origin. A preflight from that origin must succeed.
        resp = client.options(
            "/health",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"

    def test_cors_rejects_unlisted_origin(self, client: TestClient) -> None:
        # Origins outside the allowlist must NOT receive an
        # Access-Control-Allow-Origin echo; ``allow_origins=["*"]``
        # would echo any inbound Origin even with ``allow_credentials=True``.
        resp = client.options(
            "/health",
            headers={
                "Origin": "http://attacker.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" not in resp.headers
