"""In-tree API clients that send a session cookie must also send the CSRF
header, otherwise the CSRF middleware answers 403 ``csrf_header``."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from bifrost.api._cookie_signing import sign_session_cookie
from bifrost.api.auth.csrf import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from bifrost.cli.config import get_settings, reset_settings

LOGOUT = "/api/v1/auth/logout"
CHAT_SMOKE = Path(__file__).resolve().parent.parent / "scripts" / "chat_smoke.py"


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("BIFROST_CORS_ORIGINS", "http://localhost:5173")
    reset_settings()
    from bifrost.api.app import create_app

    yield create_app()
    reset_settings()


def _runner_client() -> httpx.AsyncClient:
    from bifrost.benchmarks.runner import _build_http_client

    return _build_http_client(settings=get_settings(), cookie_value=sign_session_cookie("a" * 32))


def _load_chat_smoke():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("chat_smoke_under_test", CHAT_SMOKE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_client_sends_csrf_header() -> None:
    client = _runner_client()
    assert client.headers[CSRF_HEADER_NAME] == CSRF_HEADER_VALUE
    assert client.cookies.get("bifrost_session")


async def test_runner_client_passes_csrf_middleware(app) -> None:  # type: ignore[no-untyped-def]
    runner = _runner_client()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        headers=runner.headers,
        cookies=runner.cookies,
    ) as ac:
        resp = await ac.post(LOGOUT)
    assert resp.status_code != 403, resp.text


async def test_cookie_without_header_is_rejected_control(app) -> None:  # type: ignore[no-untyped-def]
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        cookies={"bifrost_session": sign_session_cookie("a" * 32)},
    ) as ac:
        resp = await ac.post(LOGOUT)
    assert resp.status_code == 403
    assert resp.json()["code"] == "csrf_header"


async def test_chat_smoke_client_passes_csrf_middleware(app) -> None:  # type: ignore[no-untyped-def]
    # The server sets bifrost_session on create and httpx replays it on the next POST.
    headers = httpx.Headers(_load_chat_smoke().HEADERS)
    assert headers[CSRF_HEADER_NAME] == CSRF_HEADER_VALUE
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        headers=headers,
        cookies={"bifrost_session": sign_session_cookie("b" * 32)},
    ) as ac:
        resp = await ac.post(LOGOUT)
    assert resp.status_code != 403, resp.text
