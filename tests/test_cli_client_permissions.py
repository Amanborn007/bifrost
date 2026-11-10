"""`claude -p` runs under ``--permission-mode dontAsk`` + an explicit ``--allowedTools``.

``bypassPermissions`` approved every tool of every inherited MCP server, so
attacker-influenced content read by an agent could drive any operator tool.
``--tools`` only filters built-ins; ``--allowedTools`` is what gates MCP tools.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

import bifrost
from bifrost.cli.config import BifrostSettings
from bifrost.cli.config_sections import McpConfig
from bifrost.errors import ConfigError
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient

SRC = Path(bifrost.__file__).parent
JIRA_GET = "mcp__atlassian__getJiraIssue"

_OK_STDOUT = (
    b'[{"type":"assistant","message":{"content":['
    b'{"type":"tool_use","name":"StructuredOutput","input":{"ok":1}}]}},'
    b'{"type":"result","is_error":false,"subtype":"success",'
    b'"result":"done","total_cost_usd":0.0,"usage":{}}]'
)


class _Proc:
    returncode = 0

    async def communicate(self, _stdin: bytes | None = None) -> tuple[bytes, bytes]:
        return _OK_STDOUT, b""

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        pass


class _Spawned(Exception):
    pass


@pytest.fixture
def argvs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []

    async def fake_exec(*argv: str, **_kw: object) -> _Proc:
        seen.append(list(argv))
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return seen


def _client(**kw: object) -> ClaudeLlmClient:
    return ClaudeLlmClient(BifrostSettings(llm_cache_enabled=False, **kw))


def _flag(argv: list[str], name: str) -> str:
    assert name in argv, f"{name} missing from {argv}"
    return argv[argv.index(name) + 1]


async def _json_call(**kw: object) -> None:
    await _client().call_with_json_schema(
        system_prompt="s", user_message="u", model="m", json_schema={"type": "object"}, **kw
    )


@pytest.mark.parametrize("inherit", [True, False])
async def test_json_mode_with_tools_uses_allowedtools_and_dontask(argvs, inherit):
    await _json_call(allowed_tools=[JIRA_GET, "Read"], inherit_user_mcp=inherit)

    (argv,) = argvs
    assert "bypassPermissions" not in argv
    assert _flag(argv, "--permission-mode") == "dontAsk"
    assert set(_flag(argv, "--allowedTools").split(",")) == {JIRA_GET, "Read"}


async def test_tools_flag_names_only_builtins(argvs):
    await _json_call(allowed_tools=[JIRA_GET, "Read"], inherit_user_mcp=True)
    assert _flag(argvs[0], "--tools") == "Read"


@pytest.mark.parametrize("inherit", [True, False])
async def test_empty_allowed_tools_means_no_tools_and_still_dontask(argvs, inherit):
    await _json_call(allowed_tools=[], inherit_user_mcp=inherit)

    (argv,) = argvs
    assert "bypassPermissions" not in argv
    assert "--allowedTools" not in argv
    assert _flag(argv, "--permission-mode") == "dontAsk"
    assert _flag(argv, "--tools") == ""


async def test_default_call_without_tools_is_dontask(argvs):
    await _json_call()
    assert "bypassPermissions" not in argvs[0]
    assert _flag(argvs[0], "--permission-mode") == "dontAsk"


async def test_inherit_mode_still_loads_user_mcp_but_never_bypasses(argvs):
    await _json_call(allowed_tools=[JIRA_GET], inherit_user_mcp=True)
    argv = argvs[0]
    assert "--strict-mcp-config" not in argv
    assert _flag(argv, "--setting-sources") == "user"
    assert "bypassPermissions" not in argv


async def test_stream_text_with_tools_uses_allowedtools_and_dontask(monkeypatch):
    captured: list[list[str]] = []

    async def fake_exec(*argv: str, **_kw: object) -> None:
        captured.append(list(argv))
        raise _Spawned

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(_Spawned):
        async for _ in _client().stream_text(
            system_prompt="s",
            user_message="u",
            model="m",
            allowed_tools=["Read", "Grep"],
            add_dirs=["/tmp"],
        ):
            pass

    (argv,) = captured
    assert "bypassPermissions" not in argv
    assert _flag(argv, "--permission-mode") == "dontAsk"
    assert _flag(argv, "--allowedTools") == "Read,Grep"


@pytest.mark.parametrize(
    "bad",
    ["{mcp:atlassian:getJiraIssue}", "", "  ", "a,b"],
)
async def test_unrendered_or_malformed_tool_names_raise_config_error(argvs, bad):
    with pytest.raises(ConfigError):
        await _json_call(allowed_tools=["Read", bad], inherit_user_mcp=True)
    assert argvs == [], "must fail before spawning"


def test_mcp_tools_for_unknown_server_is_a_typed_error():
    with pytest.raises(ConfigError, match="Unknown MCP server"):
        McpConfig().tools("nope", ["x"])
    with pytest.raises(ConfigError, match="Unknown MCP server"):
        McpConfig().render("{mcp:nope:x}")


def test_mcp_tools_with_no_names_is_not_a_silent_empty_list():
    with pytest.raises(ConfigError):
        McpConfig().tools("atlassian", [])


# ── call sites render their list from McpConfig ──────────────────────


class _SpyClient:
    calls: list[dict[str, object]] = []

    def __init__(self, _settings: object) -> None:
        pass

    async def call_with_json_schema(self, **kw: object):
        type(self).calls.append(kw)
        raise ClaudeCliError("stop")

    call_with_dynamic_max_turns = call_with_json_schema


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> type[_SpyClient]:
    _SpyClient.calls = []
    return _SpyClient


async def test_external_agent_allowed_tools_come_from_configured_prefix(monkeypatch, spy):
    from bifrost.research import external_agent

    monkeypatch.setattr(external_agent, "ClaudeLlmClient", spy)
    settings = BifrostSettings(mcp={"tool_prefix": {"atlassian": "mcp__corp_jira__"}})

    await external_agent.research_external(question="how does X work?", settings=settings)

    (call,) = spy.calls
    tools = call["allowed_tools"]
    assert tools and all(t.startswith("mcp__corp_jira__") for t in tools), tools
    assert call["inherit_user_mcp"] is True


async def test_slack_draft_allowed_tools_come_from_configured_prefix(monkeypatch, spy):
    from bifrost.collaboration import slack_draft

    monkeypatch.setattr(slack_draft, "ClaudeLlmClient", spy)
    settings = BifrostSettings(mcp={"tool_prefix": {"slack": "mcp__corp_slack__"}})

    await slack_draft.create_slack_draft(channel="#c", body_md="hello", settings=settings)

    (call,) = spy.calls
    tools = call["allowed_tools"]
    assert tools and all(t.startswith("mcp__corp_slack__") for t in tools), tools


def _inherit_call_sites() -> list[tuple[Path, ast.Call]]:
    found = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            flag = kw.get("inherit_user_mcp")
            if isinstance(flag, ast.Constant) and flag.value is True:
                found.append((path, node))
    return found


def test_there_are_inherit_call_sites_to_check() -> None:
    assert len(_inherit_call_sites()) >= 10


def test_every_inherit_user_mcp_call_site_passes_an_explicit_rendered_tool_list() -> None:
    bad = []
    for path, call in _inherit_call_sites():
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        value = kw.get("allowed_tools")
        where = f"{path.relative_to(SRC)}:{call.lineno}"
        if value is None or ast.unparse(value) in {"[]", "()", "None"}:
            bad.append(f"{where}: no explicit allowed_tools")
        elif "mcp.tools(" not in ast.unparse(value):
            bad.append(f"{where}: allowed_tools not rendered from McpConfig: {ast.unparse(value)}")
    assert not bad, "\n".join(bad)
