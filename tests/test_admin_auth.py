"""Tests for the admin role gate.

``require_role("admin")`` replaced the shared-secret ``require_admin_token``
dependency. The gate reads the principal the identity middleware resolved,
so a request with no principal is a 401, a member is a 403, and an admin
passes. Every decision is written to the audit log with the actor.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import bifrost.api.audit as _audit_mod
from bifrost.api.auth.principal import Principal
from bifrost.api.auth.roles import RoleRequired
from bifrost.api.deps import require_role


def _make_request(
    *,
    principal: Principal | None,
    path: str = "/api/v1/admin/learning",
    method: str = "GET",
) -> Request:
    """Build a minimal real Starlette Request for the gate to introspect
    (it reads url.path, method, client.host, headers, state)."""
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 54321),
        "server": ("test", 80),
        "scheme": "http",
    }
    request = Request(scope)
    if principal is not None:
        request.state.principal = principal
    return request


@pytest.fixture
def audit_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Capture audit writes so the gate logic is tested without a DB."""
    events: list[dict[str, object]] = []

    async def _record_event(**kwargs: object) -> None:
        events.append(kwargs)

    monkeypatch.setattr(_audit_mod, "record_event", _record_event)
    return events


MEMBER = Principal(email="m@example.com", roles=("member",), source="sso_header")
ADMIN = Principal(email="a@example.com", roles=("member", "admin"), source="sso_header")


class TestRequireAdminRole:
    @pytest.mark.asyncio
    async def test_401_when_no_principal(self, audit_events) -> None:
        """No resolved principal (middleware did not run) is a 401."""
        with pytest.raises(HTTPException) as exc:
            await require_role("admin")(_make_request(principal=None))
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_403_for_a_member(self, audit_events) -> None:
        with pytest.raises(RoleRequired) as exc:
            await require_role("admin")(_make_request(principal=MEMBER))
        assert exc.value.http_status == 403
        assert exc.value.code == "auth.role_required"
        assert [(e["event_type"], e["actor"]) for e in audit_events] == [
            ("admin.access.denied", "m@example.com")
        ]

    @pytest.mark.asyncio
    async def test_raw_admin_group_without_the_role_is_refused(self, audit_events) -> None:
        """Group names carry no authority; only the resolved role does."""
        principal = Principal(email="g@example.com", groups=("bifrost-admin",), roles=("member",))
        with pytest.raises(RoleRequired):
            await require_role("admin")(_make_request(principal=principal))

    @pytest.mark.asyncio
    async def test_passes_for_an_admin_and_audits_the_actor(self, audit_events) -> None:
        returned = await require_role("admin")(_make_request(principal=ADMIN))
        assert returned is ADMIN
        assert [(e["event_type"], e["actor"]) for e in audit_events] == [
            ("admin.access.granted", "a@example.com")
        ]

    def test_unknown_role_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="superuser"):
            require_role("superuser")

    def test_gate_is_cached_so_fastapi_runs_it_once_per_request(self) -> None:
        assert require_role("admin") is require_role("admin")


class TestAdminRouterDependency:
    """Asserts the router-level dependency is wired so all admin routes
    inherit the gate. A future PR that drops the dependencies= clause
    fails this test."""

    def test_admin_router_has_gate(self) -> None:
        from bifrost.api.routes import admin

        deps = admin.router.dependencies
        assert len(deps) >= 1, "admin router must declare at least one dependency"
        assert any(getattr(d.dependency, "required_role", None) == "admin" for d in deps)
