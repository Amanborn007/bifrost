"""Session cookies and conversation ownership: orphan routes are gone,
cookie signing is strict, and the ownership skip is keyed on the provider."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bifrost.api import _cookie_signing as cs
from bifrost.api.app import create_app
from bifrost.api.deps import get_repository, get_session_factory
from bifrost.cli.config import get_settings, reset_settings
from bifrost.storage.models import Conversation
from tests.auth_helpers import boot, client, header_identity


@pytest.fixture(autouse=True)
def _reset():
    reset_settings()
    cs.reset_signing_key()
    yield
    reset_settings()
    cs.reset_signing_key()


# ── cookie signing ──────────────────────────────────────────────


def test_bare_32_hex_cookie_is_not_a_session() -> None:
    assert cs.parse_signed_cookie("a" * 32) is None


def test_bad_signature_cookie_is_not_a_session() -> None:
    assert cs.parse_signed_cookie("a" * 32 + ".AAAAAAAAAAAA") is None


def test_valid_signed_cookie_still_round_trips() -> None:
    cookie = cs.mint_signed_cookie()
    sid = cs.parse_signed_cookie(cookie)
    assert sid is not None and cookie.startswith(sid)


def test_legacy_recovery_helpers_are_gone() -> None:
    for name in ("is_unsigned_legacy", "is_stale_signed", "mint_signed_cookie_from"):
        assert not hasattr(cs, name)


def _list_stub(app: Any) -> None:
    async def _repo() -> AsyncIterator[Any]:
        mock = MagicMock()
        mock.list_conversations_with_summary = AsyncMock(return_value=[])
        yield mock

    app.dependency_overrides[get_repository] = _repo


@pytest.mark.parametrize("kind", ["bare", "bad_signature"])
async def test_http_request_with_unacceptable_cookie_gets_a_fresh_session(
    monkeypatch, kind: str
) -> None:
    app = boot(monkeypatch, provider="none", demo=True)
    _list_stub(app)
    old_id = uuid.uuid4().hex
    cookie = old_id if kind == "bare" else f"{old_id}.forged"
    async with client(app) as ac:
        resp = await ac.get(
            "/api/v1/conversations", headers={"Cookie": f"bifrost_session={cookie}"}
        )
    assert resp.status_code == 200
    set_cookie = resp.headers["set-cookie"]
    assert set_cookie.startswith("bifrost_session=")
    assert old_id not in set_cookie


async def test_http_request_with_valid_cookie_keeps_its_session(monkeypatch) -> None:
    app = boot(monkeypatch, provider="none", demo=True)
    _list_stub(app)
    async with client(app) as ac:
        resp = await ac.get(
            "/api/v1/conversations",
            headers={"Cookie": f"bifrost_session={cs.mint_signed_cookie()}"},
        )
    assert resp.status_code == 200
    assert "set-cookie" not in resp.headers


# ── orphan routes ───────────────────────────────────────────────


def test_orphan_routes_are_not_registered() -> None:
    paths = {getattr(r, "path", "") for r in create_app().routes}
    assert "/api/v1/conversations/orphans" not in paths
    assert "/api/v1/conversations/adopt-orphan" not in paths


async def test_orphan_endpoints_are_not_served(monkeypatch) -> None:
    app = boot(monkeypatch, provider="none", demo=True)
    _list_stub(app)
    async with client(app) as ac:
        listing = await ac.get("/api/v1/conversations/orphans")
        adopt = await ac.post(
            "/api/v1/conversations/adopt-orphan",
            json={"session_id": "a" * 32},
            headers={"X-Requested-With": "bifrost-ui"},
        )
    # The path now falls into the sibling /{conversation_id} routes: a
    # non-UUID id is a 400 for GET, and POST has no such sibling (405).
    assert listing.status_code in (400, 404)
    assert adopt.status_code in (404, 405)


def test_orphan_repository_helpers_are_gone() -> None:
    from bifrost.storage.repository import EntityRepository

    assert not hasattr(EntityRepository, "list_orphan_sessions")
    assert not hasattr(EntityRepository, "adopt_orphan_session")


# ── ownership skip keyed on the provider ────────────────────────


@pytest.fixture
async def foreign_conversation(monkeypatch) -> AsyncIterator[uuid.UUID]:
    """A committed conversation owned by some other session."""
    monkeypatch.setenv("BIFROST_DEMO_MODE", "true")
    reset_settings()
    factory = get_session_factory(get_settings())
    conv_id = uuid.uuid4()
    async with factory() as session:
        session.add(Conversation(id=conv_id, session_id="f" * 32, title="someone else's chat"))
        await session.commit()
    yield conv_id
    async with factory() as session:
        row = await session.get(Conversation, conv_id)
        if row is not None:
            await session.delete(row)
            await session.commit()


async def test_operator_email_under_header_provider_does_not_expose_other_sessions(
    monkeypatch, foreign_conversation: uuid.UUID
) -> None:
    app = boot(
        monkeypatch,
        provider="header",
        roles={"admin": []},
        extra_env={"BIFROST_OPERATOR_EMAIL": "op@example.com"},
    )
    async with client(app) as ac:
        resp = await ac.get(
            f"/api/v1/conversations/{foreign_conversation}",
            headers={
                **header_identity("u@example.com"),
                "Cookie": f"bifrost_session={cs.mint_signed_cookie()}",
            },
        )
    assert resp.status_code == 404


async def test_none_provider_operator_sees_every_conversation(
    monkeypatch, foreign_conversation: uuid.UUID
) -> None:
    app = boot(monkeypatch, provider="none", operator_email="op@example.com")
    async with client(app) as ac:
        resp = await ac.get(
            f"/api/v1/conversations/{foreign_conversation}",
            headers={"Cookie": f"bifrost_session={cs.mint_signed_cookie()}"},
        )
    assert resp.status_code == 200, resp.text
