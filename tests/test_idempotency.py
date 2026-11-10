"""End-to-end test for the GET /research-status endpoint.

Tests the resumability state machine via the HTTP layer:
- Empty conversation returns nulls for both in_flight and latest.
- A done run shows up as latest.
- 404/400 are returned for missing/malformed conversation ids.

Uses httpx ASGITransport so the entire test (DB setup + HTTP request)
runs on a single event loop — avoids asyncpg's "different loop" /
"another operation in progress" errors that come up when mixing
``conftest.py`` session-scoped engines with FastAPI's TestClient
(which spins up anyio's portal on a worker thread).
"""

from __future__ import annotations

import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bifrost.api._cookie_signing import mint_signed_cookie, parse_signed_cookie
from bifrost.api.app import create_app
from bifrost.cli.config import reset_settings
from bifrost.research.runs import mark_done, start_run
from bifrost.storage.models import Base
from bifrost.storage.repository import EntityRepository


def _signed_session_pair() -> tuple[str, str]:
    """Return (session_id, signed_cookie_value) so tests can create a
    conversation under a session id that the API will accept on the
    next request."""
    cookie = mint_signed_cookie()
    parsed = parse_signed_cookie(cookie)
    assert parsed is not None
    return parsed, cookie


TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://bifrost:bifrost_dev@localhost:5432/bifrost_test",
)


def _patch_session_factory(monkeypatch, factory) -> None:
    """Make every API code path that calls ``get_session_factory(settings)``
    return our test session_factory.
    """
    import bifrost.api.deps as deps

    monkeypatch.setattr(
        deps,
        "get_session_factory",
        lambda _settings: factory,
    )


@pytest.fixture
async def _api_factory():
    """Per-test fresh engine + factory. Schema is owned by the conftest
    fixture, but we open a dedicated engine here so connections are
    bound to the test's running event loop. ``await eng.dispose()``
    in teardown ensures asyncpg connections close cleanly."""
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    yield factory
    await eng.dispose()


def _make_app():
    """Build the FastAPI app with the lifespan disabled — startup
    hooks (worker, auth probe, orphan reclaim) need full DB +
    subprocess context that the test environment can't supply."""
    app = create_app()
    app.router.lifespan_context = None  # type: ignore[assignment]
    return app


async def _create_conversation(
    factory: async_sessionmaker,
    *,
    session_id: str,
) -> uuid.UUID:
    async with factory() as session:
        repo = EntityRepository(session)
        conv = await repo.create_conversation(
            session_id=session_id,
            title="idem test",
            scope_repo_ids=[],
        )
        await session.commit()
        return conv.id


@pytest.mark.asyncio
async def test_research_status_endpoint_returns_null_for_fresh_conv(
    _api_factory,
    monkeypatch,
) -> None:
    """A conversation that has never run any research should return
    both fields as null."""
    reset_settings()
    sid, cookie = _signed_session_pair()
    cid = await _create_conversation(_api_factory, session_id=sid)
    _patch_session_factory(monkeypatch, _api_factory)

    transport = ASGITransport(app=_make_app())
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"bifrost_session": cookie},
    ) as client:
        resp = await client.get(
            f"/api/v1/conversations/{cid}/research-status",
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["in_flight"] is None
    assert body["data"]["latest"] is None


@pytest.mark.asyncio
async def test_research_status_returns_latest_done_run(
    _api_factory,
    monkeypatch,
) -> None:
    """After one done run, status returns it as latest (not in_flight)."""
    reset_settings()
    sid, cookie = _signed_session_pair()
    cid = await _create_conversation(_api_factory, session_id=sid)

    run_id = await start_run(
        _api_factory,
        conversation_id=cid,
        turn_id=1,
    )
    await mark_done(
        _api_factory,
        run_id,
        final_answer_text="cached answer 42.",
        total_cost_usd=1.50,
    )

    _patch_session_factory(monkeypatch, _api_factory)

    transport = ASGITransport(app=_make_app())
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"bifrost_session": cookie},
    ) as client:
        resp = await client.get(
            f"/api/v1/conversations/{cid}/research-status",
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["in_flight"] is None
    latest = body["data"]["latest"]
    assert latest is not None
    assert latest["status"] == "done"
    assert latest["final_answer_text"] == "cached answer 42."
    assert latest["total_cost_usd"] == 1.5


@pytest.mark.asyncio
async def test_research_status_404_for_unknown_conversation(
    _api_factory,
    monkeypatch,
) -> None:
    """An unknown conversation id returns 404 — no leak of run state."""
    reset_settings()
    _patch_session_factory(monkeypatch, _api_factory)
    _, cookie = _signed_session_pair()
    transport = ASGITransport(app=_make_app())
    bogus = uuid.uuid4()
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"bifrost_session": cookie},
    ) as client:
        resp = await client.get(
            f"/api/v1/conversations/{bogus}/research-status",
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_research_status_400_for_malformed_id(_api_factory, monkeypatch) -> None:
    """A malformed UUID returns 400, not 500."""
    reset_settings()
    _patch_session_factory(monkeypatch, _api_factory)
    _, cookie = _signed_session_pair()
    transport = ASGITransport(app=_make_app())
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"bifrost_session": cookie},
    ) as client:
        resp = await client.get(
            "/api/v1/conversations/not-a-uuid/research-status",
        )
    assert resp.status_code == 400
