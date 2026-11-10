"""Jira URL building without a database: config helper, row mappers, write fallbacks."""

from __future__ import annotations

from pathlib import Path

import pytest

from bifrost.cli.config import reset_settings
from bifrost.cli.config_sections import JiraSourceConfig

BASE = "https://acme.atlassian.net"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIFROST_CONFIG", "")
    monkeypatch.delenv("BIFROST_SOURCES__JIRA__BASE_URL", raising=False)
    reset_settings()
    yield
    monkeypatch.undo()
    reset_settings()


@pytest.fixture
def with_base_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BIFROST_SOURCES__JIRA__BASE_URL", BASE)
    reset_settings()


# ── JiraSourceConfig.browse_url ─────────────────────────────────────────


@pytest.mark.parametrize("key", [None, ""])
def test_browse_url_without_a_key_is_none(key: str | None) -> None:
    assert JiraSourceConfig(base_url=BASE).browse_url(key) is None


def test_browse_url_without_base_url_is_none() -> None:
    assert JiraSourceConfig().browse_url("ABC-1") is None


def test_browse_url_joins_base_and_key_ignoring_trailing_slash() -> None:
    assert JiraSourceConfig(base_url=BASE + "/").browse_url("ABC-1") == f"{BASE}/browse/ABC-1"


# ── row mappers ─────────────────────────────────────────────────────────


def _inbox_row(**over):
    row = {
        "id": "t1",
        "title": "t",
        "state": "draft_intent",
        "jira_key": "ABC-1",
        "pr_url": None,
        "pr_number": None,
        "updated_at": None,
    }
    return {**row, **over}


def _pulse_row(**over):
    return {**_inbox_row(), "owner_email": "a@example.com", **over}


def test_inbox_task_row_jira_url_default_is_none() -> None:
    from bifrost.actions.personal_inbox import _task_row

    assert _task_row(_inbox_row()).jira_url is None


def test_inbox_task_row_jira_url_with_base_url(with_base_url) -> None:
    from bifrost.actions.personal_inbox import _task_row

    assert _task_row(_inbox_row()).jira_url == f"{BASE}/browse/ABC-1"


def test_inbox_task_row_without_a_key_has_no_url(with_base_url) -> None:
    from bifrost.actions.personal_inbox import _task_row

    assert _task_row(_inbox_row(jira_key=None)).jira_url is None


def test_pulse_task_row_jira_url_default_is_none() -> None:
    from bifrost.actions.squad_pulse import TaskRow

    assert TaskRow.from_db(_pulse_row()).jira_url is None


def test_pulse_task_row_jira_url_with_base_url(with_base_url) -> None:
    from bifrost.actions.squad_pulse import TaskRow

    assert TaskRow.from_db(_pulse_row()).jira_url == f"{BASE}/browse/ABC-1"


def test_pulse_task_row_without_a_key_has_no_url(with_base_url) -> None:
    from bifrost.actions.squad_pulse import TaskRow

    assert TaskRow.from_db(_pulse_row(jira_key=None)).jira_url is None


# ── writer + indexer fallbacks ──────────────────────────────────────────


def test_canonical_url_prefers_the_url_the_tool_returned() -> None:
    from bifrost.actions.jira_writer import _canonical_url

    got = _canonical_url(
        "ABC-1", "https://jira.example/browse/ABC-1", JiraSourceConfig(base_url=BASE)
    )
    assert got == "https://jira.example/browse/ABC-1"


def test_canonical_url_builds_from_base_url_when_tool_returned_none() -> None:
    from bifrost.actions.jira_writer import _canonical_url

    assert _canonical_url("ABC-1", "", JiraSourceConfig(base_url=BASE)) == f"{BASE}/browse/ABC-1"


def test_canonical_url_is_empty_without_base_url() -> None:
    from bifrost.actions.jira_writer import _canonical_url

    assert _canonical_url("ABC-1", "", JiraSourceConfig()) == ""


def test_indexer_keeps_the_url_the_model_returned() -> None:
    from bifrost.research.external_indexer import _resolve_issue_url

    got = _resolve_issue_url("https://x/browse/ABC-1", "ABC-1", JiraSourceConfig(base_url=BASE))
    assert got == "https://x/browse/ABC-1"


def test_indexer_fills_an_empty_url_from_browse_url() -> None:
    from bifrost.research.external_indexer import _resolve_issue_url

    assert (
        _resolve_issue_url("", "ABC-1", JiraSourceConfig(base_url=BASE)) == f"{BASE}/browse/ABC-1"
    )


def test_indexer_leaves_url_empty_without_base_url() -> None:
    from bifrost.research.external_indexer import _resolve_issue_url

    assert _resolve_issue_url("", "ABC-1", JiraSourceConfig()) == ""
