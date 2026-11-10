"""Every LLM prompt / allowed-tools list that names an MCP tool is config-driven.

The golden file ``fixtures/mcp_surfaces_golden.json`` was captured from the tree
before prompts were templated; with the default ``McpConfig`` every surface must
still render byte-for-byte the same. Custom prefixes must flow through, and they
must be read at call time (settings singleton), never frozen at import.
"""

from __future__ import annotations

import importlib
import json
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from bifrost.cli.config import BifrostSettings, reset_settings
from bifrost.cli.config_sections import McpConfig
from bifrost.errors import ConfigError
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_PATH = Path(__file__).parent / "fixtures" / "mcp_surfaces_golden.json"
ACME_JIRA = "https://acme.atlassian.net"
CHANNELS = ("#orders-eng", "#eng-handovers")

DEFAULT_PREFIXES = {
    "atlassian": "mcp__atlassian__",
    "github": "mcp__github__",
    "slack": "mcp__claude_ai_Slack__",
    "slack_write": "mcp__plugin_slack_slack__",
    "gdrive": "mcp__claude_ai_Google_Drive__",
}
CUSTOM_PREFIXES = {
    "atlassian": "mcp__corp_atl__",
    "github": "mcp__corp_gh__",
    "slack": "mcp__corp_slack__",
    "slack_write": "mcp__corp_slackw__",
    "gdrive": "mcp__corp_drive__",
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIFROST_CONFIG", "")
    monkeypatch.delenv("BIFROST_MCP__TOOL_PREFIX", raising=False)
    monkeypatch.delenv("BIFROST_SOURCES__JIRA__BASE_URL", raising=False)
    reset_settings()
    yield
    reset_settings()


def _settings(
    *, prefixes: dict[str, str] | None = None, base_url: str = ACME_JIRA
) -> BifrostSettings:
    kw: dict[str, Any] = {"sources": {"jira": {"base_url": base_url}}}
    if prefixes:
        kw["mcp"] = {"tool_prefix": prefixes}
    return BifrostSettings(**kw)


def _golden() -> dict[str, dict[str, Any]]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _recoloured(value: Any, prefixes: dict[str, str]) -> Any:
    """Golden text/tool list with each default prefix swapped for ``prefixes[server]``."""
    if isinstance(value, list):
        return [_recoloured(v, prefixes) for v in value]
    if isinstance(value, str):
        for server, default in DEFAULT_PREFIXES.items():
            value = value.replace(default, prefixes[server])
        return value
    return value


# ── recorder: capture what every agent hands the LLM client ─────────────


@contextmanager
def _recording():
    calls: list[dict[str, Any]] = []

    async def _fake(self: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        raise ClaudeCliError("recorded")

    with (
        patch.object(ClaudeLlmClient, "call_with_json_schema", _fake),
        patch.object(ClaudeLlmClient, "call_with_dynamic_max_turns", _fake),
    ):
        yield calls


def _repo_stub() -> SimpleNamespace:
    return SimpleNamespace(id="rid", name="widgets", urn="urn:bifrost:repo:widgets")


async def _drive_confluence(s: BifrostSettings | None) -> None:
    from bifrost.research.confluence_agent import research_confluence_for_repo

    await research_confluence_for_repo(
        question="q", repo_id="r", repo_name="widgets", confluence_space_key="SP", settings=s
    )


async def _drive_index_confluence(s: BifrostSettings | None) -> None:
    from bifrost.research.external_indexer import index_confluence_for_repo

    await index_confluence_for_repo(
        session=None,
        repo=_repo_stub(),
        space_key="SP",
        settings=s,  # type: ignore[arg-type]
    )


async def _drive_index_jira(s: BifrostSettings | None) -> None:
    from bifrost.research.external_indexer import index_jira_for_repo

    await index_jira_for_repo(
        session=None,
        repo=_repo_stub(),
        project_key="PRJ",
        settings=s,  # type: ignore[arg-type]
    )


async def _drive_github(s: BifrostSettings | None) -> None:
    from bifrost.research.github_agent import research_github_for_repo

    await research_github_for_repo(
        question="q",
        repo_id="r",
        repo_name="widgets",
        repo_url="https://github.com/acme/widgets",
        settings=s,
    )


async def _drive_slack_agent(s: BifrostSettings | None) -> None:
    from bifrost.research.slack_agent import research_slack_for_repo

    await research_slack_for_repo(
        question="q", repo_id="r", repo_name="widgets", slack_channel="#widgets", settings=s
    )


async def _drive_jira_agent(s: BifrostSettings | None) -> None:
    from bifrost.research.jira_agent import research_jira_for_repo

    await research_jira_for_repo(
        question="q", repo_id="r", repo_name="widgets", jira_project_key="PRJ", settings=s
    )


async def _drive_gdocs_agent(s: BifrostSettings | None) -> None:
    from bifrost.research.gdocs_agent import research_gdocs_for_repo

    await research_gdocs_for_repo(
        question="q", repo_id="r", repo_name="widgets", gdoc_folder_url=None, settings=s
    )


async def _drive_gdocs_index(s: BifrostSettings | None) -> None:
    from bifrost.cli.config import get_settings
    from bifrost.research.gdocs_index import _index_one_repo

    await _index_one_repo(
        repo=_repo_stub(),  # type: ignore[arg-type]
        settings=s or get_settings(),
        cutoff=datetime(2026, 1, 1),
        max_docs=5,
        folder_threshold=3,
    )


async def _drive_external_agent(s: BifrostSettings | None) -> None:
    from bifrost.research.external_agent import research_external

    await research_external(question="q", settings=s)


async def _drive_jira_chain(s: BifrostSettings | None) -> None:
    from bifrost.cli.config import get_settings
    from bifrost.research.coordinator import _prefetch_jira_chain

    await _prefetch_jira_chain("what is the state of ABC-123?", s or get_settings())


async def _drive_jira_writer(s: BifrostSettings | None) -> None:
    from bifrost.actions.jira_writer import JiraWriteFailed, _run_jira_write

    with pytest.raises(JiraWriteFailed):
        await _run_jira_write(user_message="m", settings=s)


def _channel_driver(kind: str, channel: str):
    async def _drive(s: BifrostSettings | None) -> None:
        from bifrost.collaboration.slack_draft import create_slack_draft

        await create_slack_draft(channel=channel, body_md="hello", settings=s)

    return _drive


DRIVERS: dict[str, Any] = {
    "confluence_agent": _drive_confluence,
    "external_indexer_confluence": _drive_index_confluence,
    "external_indexer_jira": _drive_index_jira,
    "github_agent": _drive_github,
    "slack_agent": _drive_slack_agent,
    "jira_agent": _drive_jira_agent,
    "gdocs_agent": _drive_gdocs_agent,
    "gdocs_index": _drive_gdocs_index,
    "external_agent": _drive_external_agent,
    "coordinator_jira_chain": _drive_jira_chain,
    "jira_writer": _drive_jira_writer,
    **{f"slack_draft:{c}": _channel_driver("slack_draft", c) for c in CHANNELS},
}


async def _capture(key: str, s: BifrostSettings | None) -> dict[str, Any]:
    with _recording() as calls:
        await DRIVERS[key](s)
    assert len(calls) == 1, f"{key}: expected exactly one LLM call, saw {len(calls)}"
    call = calls[0]
    return {
        "system_prompt": call["system_prompt"],
        "allowed_tools": list(call["allowed_tools"]),
    }


# ── (ii) recorder level: what is really sent ────────────────────────────


@pytest.mark.parametrize("key", sorted(DRIVERS))
async def test_default_config_sends_golden(key: str) -> None:
    got = await _capture(key, _settings())
    want = _golden()[key]
    assert got["system_prompt"] == want["system_prompt"]
    assert got["allowed_tools"] == want["allowed_tools"]
    assert "{mcp:" not in got["system_prompt"]


@pytest.mark.parametrize("key", sorted(DRIVERS))
async def test_custom_prefixes_flow_into_every_surface(key: str) -> None:
    got = await _capture(key, _settings(prefixes=CUSTOM_PREFIXES))
    want = _golden()[key]
    assert got["system_prompt"] == _recoloured(want["system_prompt"], CUSTOM_PREFIXES)
    assert got["allowed_tools"] == _recoloured(want["allowed_tools"], CUSTOM_PREFIXES)
    blob = got["system_prompt"] + " ".join(got["allowed_tools"])
    for default in DEFAULT_PREFIXES.values():
        assert default not in blob


def test_golden_covers_every_driver() -> None:
    assert set(_golden()) == set(DRIVERS)


# ── (i) builder level ───────────────────────────────────────────────────

# key -> (module, server, prompt constant, tool-name constant)
TEMPLATED: dict[str, tuple[str, str, str, str]] = {
    "confluence_agent": (
        "bifrost.research.confluence_agent",
        "atlassian",
        "_CONFLUENCE_SYSTEM_PROMPT",
        "_CONFLUENCE_READ_TOOL_NAMES",
    ),
    "github_agent": (
        "bifrost.research.github_agent",
        "github",
        "_GITHUB_SYSTEM_PROMPT",
        "_GITHUB_READ_TOOL_NAMES",
    ),
    "slack_agent": (
        "bifrost.research.slack_agent",
        "slack",
        "_SLACK_SYSTEM_PROMPT",
        "_SLACK_READ_TOOL_NAMES",
    ),
    "jira_agent": (
        "bifrost.research.jira_agent",
        "atlassian",
        "_JIRA_SYSTEM_PROMPT",
        "_JIRA_READ_TOOL_NAMES",
    ),
    "gdocs_agent": (
        "bifrost.research.gdocs_agent",
        "gdrive",
        "_GDOCS_SYSTEM_PROMPT",
        "_GDOCS_READ_TOOL_NAMES",
    ),
    "gdocs_index": (
        "bifrost.research.gdocs_index",
        "gdrive",
        "_GDOCS_INDEX_SYSTEM_PROMPT",
        "_GDOCS_INDEX_TOOL_NAMES",
    ),
    "external_agent": (
        "bifrost.research.external_agent",
        "atlassian",
        "_EXTERNAL_SYSTEM_PROMPT",
        "_ATLASSIAN_READONLY_TOOL_NAMES",
    ),
    "coordinator_jira_chain": (
        "bifrost.research.coordinator",
        "atlassian",
        "_JIRA_CHAIN_SYSTEM",
        "_JIRA_CHAIN_TOOL_NAMES",
    ),
    "jira_writer": (
        "bifrost.actions.jira_writer",
        "atlassian",
        "_JIRA_WRITE_SYSTEM",
        "_JIRA_WRITE_TOOL_NAMES",
    ),
}


@pytest.mark.parametrize("key", sorted(TEMPLATED))
def test_builder_level_matches_golden(key: str) -> None:
    module, server, prompt_attr, names_attr = TEMPLATED[key]
    mod = importlib.import_module(module)
    s = _settings()
    want = _golden()[key]
    assert s.mcp.render(getattr(mod, prompt_attr)) == want["system_prompt"]
    prefix = DEFAULT_PREFIXES[server]
    assert s.mcp.tools(server, getattr(mod, names_attr)) == [
        t for t in want["allowed_tools"] if t.startswith(prefix)
    ]


@pytest.mark.parametrize("key", sorted(TEMPLATED))
def test_prompt_constants_are_token_templates(key: str) -> None:
    module, _server, prompt_attr, names_attr = TEMPLATED[key]
    mod = importlib.import_module(module)
    assert isinstance(getattr(mod, prompt_attr), str)
    assert all(not n.startswith("mcp__") for n in getattr(mod, names_attr))


@pytest.mark.parametrize("which", ["conf", "jira"])
def test_index_prompts_builder_level(which: str) -> None:
    from bifrost.research import external_indexer as ix

    s = _settings()
    build = ix._conf_index_system_prompt if which == "conf" else ix._jira_index_system_prompt
    key = "external_indexer_confluence" if which == "conf" else "external_indexer_jira"
    assert build(s) == _golden()[key]["system_prompt"]
    assert s.mcp.tools("atlassian", ix._ATLASSIAN_INDEX_TOOL_NAMES) == [
        t for t in _golden()[key]["allowed_tools"] if t.startswith(DEFAULT_PREFIXES["atlassian"])
    ]


@pytest.mark.parametrize("which", ["conf", "jira"])
def test_index_prompts_without_base_url_name_no_atlassian_host(which: str) -> None:
    from bifrost.research import external_indexer as ix

    s = _settings(base_url="")
    build = ix._conf_index_system_prompt if which == "conf" else ix._jira_index_system_prompt
    prompt = build(s)
    assert "atlassian.net" not in prompt
    assert "{mcp:" not in prompt
    if which == "conf":
        assert re.search(r'url\s+← result\.webUrl \(or ""\)', prompt)
    else:
        assert re.search(r'url\s+← ""', prompt)


def test_slack_builders_take_mcp_and_default_to_settings() -> None:
    from bifrost.collaboration import slack_draft

    g = _golden()
    s = _settings()
    for ch in CHANNELS:
        assert (
            slack_draft._build_system_prompt(ch, s.mcp) == g[f"slack_draft:{ch}"]["system_prompt"]
        )
        assert slack_draft._build_system_prompt(ch) == g[f"slack_draft:{ch}"]["system_prompt"]


def test_slack_tool_constants_are_bare_names_still_inside_the_prompt() -> None:
    from bifrost.collaboration import slack_draft

    sp = slack_draft._build_system_prompt("#orders-eng")
    assert not slack_draft._SLACK_SEARCH_TOOL.startswith("mcp__")
    assert slack_draft._SLACK_SEARCH_TOOL in sp
    assert slack_draft._SLACK_DRAFT_TOOL in sp


# ── (iv) read at call time, not import time ─────────────────────────────


async def test_prefix_is_read_at_call_time(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from bifrost.collaboration import slack_draft

    cfg = tmp_path / "bifrost.yaml"
    cfg.write_text(
        "mcp:\n  tool_prefix:\n    atlassian: mcp__late_bound__\n    slack: mcp__late_slack__\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BIFROST_CONFIG", str(cfg))
    reset_settings()  # after every module above was imported

    got = await _capture("confluence_agent", None)
    assert all(t.startswith("mcp__late_bound__") for t in got["allowed_tools"])
    assert "mcp__late_bound__searchConfluenceUsingCql" in got["system_prompt"]
    assert "mcp__atlassian__" not in got["system_prompt"]

    sp = slack_draft._build_system_prompt("#x")
    assert "mcp__late_slack__slack_send_message_draft" in sp
    assert "mcp__claude_ai_Slack__" not in sp


# ── (v) source guard ────────────────────────────────────────────────────


def test_no_mcp_tool_names_hardcoded_outside_config_sections() -> None:
    offenders: list[str] = []
    for path in (REPO_ROOT / "src").rglob("*"):
        if not path.is_file() or path.suffix not in {
            ".py",
            ".txt",
            ".md",
            ".yaml",
            ".yml",
            ".json",
        }:
            continue
        if path.name == "config_sections.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "mcp__" in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, offenders


# ── McpConfig.tools / render ────────────────────────────────────────────


def test_tools_prefixes_each_name() -> None:
    assert McpConfig().tools("atlassian", ["getJiraIssue", "search"]) == [
        "mcp__atlassian__getJiraIssue",
        "mcp__atlassian__search",
    ]


def test_tools_accepts_a_tuple_and_returns_a_list() -> None:
    out = McpConfig().tools("github", ("list_issues",))
    assert out == ["mcp__github__list_issues"]
    assert isinstance(out, list)


def test_tools_unknown_server_is_config_error() -> None:
    with pytest.raises(ConfigError):
        McpConfig().tools("nope", ["x"])


def test_render_replaces_every_token_with_the_configured_prefix() -> None:
    mcp = McpConfig(tool_prefix={"atlassian": "mcp__corp__"})
    text = "a {mcp:atlassian:getJiraIssue} b {mcp:slack:slack_search_public} c {mcp:atlassian:getJiraIssue}"
    assert mcp.render(text) == (
        "a mcp__corp__getJiraIssue b mcp__claude_ai_Slack__slack_search_public c mcp__corp__getJiraIssue"
    )


def test_render_leaves_other_braces_alone() -> None:
    text = 'json {"a": 1} and {channel} and {mcp} and {mcp:atlassian}'
    assert McpConfig().render(text) == text


def test_render_unknown_server_is_config_error() -> None:
    with pytest.raises(ConfigError):
        McpConfig().render("use {mcp:nope:thing}")


def test_render_accepts_hyphenated_tool_names() -> None:
    assert McpConfig().render("{mcp:github:get-issue}") == "mcp__github__get-issue"
