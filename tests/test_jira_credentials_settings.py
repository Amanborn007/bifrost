"""Jira live-fetch credentials come from ``sources.jira`` settings, never raw JIRA_* env."""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from bifrost.actions import jira_enrich
from bifrost.cli.config import BifrostSettings, reset_settings, validate_startup
from bifrost.cli.config_sections import JiraSourceConfig
from bifrost.errors import ConfigError

TOKEN = "s3cr3t-token-value"
EMAIL = "bot@acme.test"
BASE = "https://jira.acme.test"
LEGACY = [
    ("JIRA_BASE_URL", "BIFROST_SOURCES__JIRA__BASE_URL"),
    ("JIRA_EMAIL", "BIFROST_SOURCES__JIRA__EMAIL"),
    ("JIRA_API_TOKEN", "BIFROST_SOURCES__JIRA__API_TOKEN"),
]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setitem(BifrostSettings.model_config, "env_file", None)
    monkeypatch.setenv("BIFROST_CONFIG", "")
    for legacy, _ in LEGACY:
        monkeypatch.delenv(legacy, raising=False)
    reset_settings()
    yield
    reset_settings()


def _settings(**jira: Any) -> BifrostSettings:
    return BifrostSettings(sources={"jira": jira})


def _configured() -> BifrostSettings:
    return _settings(base_url=BASE, email=EMAIL, api_token=TOKEN)


def _mock_jira(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("bifrost.adapters.jira.httpx.AsyncClient", factory)


ISSUE = {
    "key": "ABC-1",
    "fields": {
        "summary": "Do the thing",
        "description": "Acceptance Criteria:\n- it works",
        "status": {"name": "Open"},
        "issuetype": {"name": "Task"},
        "labels": [],
        "components": [],
    },
}


# ── settings fields ─────────────────────────────────────────────────────


def test_defaults_are_unset() -> None:
    cfg = JiraSourceConfig()
    assert cfg.email is None
    assert cfg.api_token is None


def test_api_token_is_secret() -> None:
    cfg = _configured().sources.jira
    assert isinstance(cfg.api_token, SecretStr)
    assert TOKEN not in repr(cfg)
    assert TOKEN not in str(_configured().model_dump())


def test_nested_env_names_are_known_and_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIFROST_SOURCES__JIRA__BASE_URL", BASE)
    monkeypatch.setenv("BIFROST_SOURCES__JIRA__EMAIL", EMAIL)
    monkeypatch.setenv("BIFROST_SOURCES__JIRA__API_TOKEN", TOKEN)
    jira = validate_startup().sources.jira
    assert (jira.base_url, jira.email) == (BASE, EMAIL)
    assert jira.api_token is not None
    assert jira.api_token.get_secret_value() == TOKEN


@pytest.mark.parametrize("given", [{"email": EMAIL}, {"api_token": TOKEN}])
def test_email_and_token_must_come_together(given: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="sources.jira.email and sources.jira.api_token"):
        _settings(**given)


# ── live fetch ──────────────────────────────────────────────────────────


async def test_live_fetch_uses_settings_base_url_and_auth(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ISSUE)

    _mock_jira(monkeypatch, handler)
    s = _configured()
    monkeypatch.setattr(jira_enrich, "get_settings", lambda: s)
    caplog.set_level(logging.DEBUG)

    result = await jira_enrich._live_fetch("ABC-1")

    assert result is not None
    assert result.title == "Do the thing"
    assert result.url == f"{BASE}/browse/ABC-1"
    assert result.source == "live"
    assert len(seen) == 1
    assert str(seen[0].url).startswith(f"{BASE}/rest/api/3/issue/ABC-1")
    want = "Basic " + base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    assert seen[0].headers["Authorization"] == want
    assert TOKEN not in caplog.text


async def test_live_fetch_failure_logs_without_the_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _mock_jira(monkeypatch, lambda request: httpx.Response(401, json={}))
    s = _configured()
    monkeypatch.setattr(jira_enrich, "get_settings", lambda: s)
    caplog.set_level(logging.DEBUG)

    assert await jira_enrich._live_fetch("ABC-1") is None
    assert "ABC-1" in caplog.text
    assert TOKEN not in caplog.text


async def test_live_fetch_ignores_raw_jira_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_BASE_URL", BASE)
    monkeypatch.setenv("JIRA_EMAIL", EMAIL)
    monkeypatch.setenv("JIRA_API_TOKEN", TOKEN)
    _mock_jira(monkeypatch, lambda request: pytest.fail("no request expected"))
    s = _settings()
    monkeypatch.setattr(jira_enrich, "get_settings", lambda: s)
    assert await jira_enrich._live_fetch("ABC-1") is None


async def test_live_fetch_without_credentials_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(base_url=BASE)
    monkeypatch.setattr(jira_enrich, "get_settings", lambda: s)
    assert await jira_enrich._live_fetch("ABC-1") is None


async def test_live_fetch_credentials_without_base_url_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(email=EMAIL, api_token=TOKEN)
    monkeypatch.setattr(jira_enrich, "get_settings", lambda: s)
    with pytest.raises(ConfigError, match="sources.jira.base_url"):
        await jira_enrich._live_fetch("ABC-1")


def test_no_credentials_note_names_the_settings() -> None:
    src = Path(jira_enrich.__file__).read_text(encoding="utf-8")
    assert "JIRA_BASE_URL" not in src.replace("BIFROST_SOURCES__JIRA__BASE_URL", "")
    assert "JIRA_API_TOKEN" not in src.replace("BIFROST_SOURCES__JIRA__API_TOKEN", "")


# ── startup rejects the old names ───────────────────────────────────────


@pytest.mark.parametrize(("legacy", "replacement"), LEGACY)
def test_raw_env_var_fails_startup_naming_the_replacement(
    monkeypatch: pytest.MonkeyPatch, legacy: str, replacement: str
) -> None:
    monkeypatch.setenv(legacy, "x")
    with pytest.raises(ConfigError) as exc:
        validate_startup()
    assert legacy in str(exc.value)
    assert replacement in str(exc.value)
    assert "environment" in str(exc.value)


@pytest.mark.parametrize(("legacy", "replacement"), LEGACY)
def test_dotenv_line_fails_startup_naming_the_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, legacy: str, replacement: str
) -> None:
    path = tmp_path / ".env"
    path.write_text(f"BIFROST_DEMO_MODE=false\n{legacy}=x\n", encoding="utf-8")
    monkeypatch.setitem(BifrostSettings.model_config, "env_file", str(path))
    with pytest.raises(ConfigError) as exc:
        validate_startup()
    assert legacy in str(exc.value)
    assert replacement in str(exc.value)
    assert ".env" in str(exc.value)


def test_error_never_contains_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_API_TOKEN", TOKEN)
    with pytest.raises(ConfigError) as exc:
        validate_startup()
    assert TOKEN not in str(exc.value)
    assert TOKEN not in repr(exc.value.details)


def test_unrelated_non_bifrost_dotenv_lines_still_boot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / ".env"
    path.write_text("SOMETHING_ELSE=abc\nBIFROST_DEMO_MODE=false\n", encoding="utf-8")
    monkeypatch.setitem(BifrostSettings.model_config, "env_file", str(path))
    validate_startup()
