"""External-knowledge agent — Confluence / Jira via MCP.

Bifrost's promise is that it doesn't only research code — it also pulls
runbooks, architecture pages, epics, and incidents from the org's wikis
and ticket systems. This module spawns a parallel sub-agent that talks
to the Atlassian MCP (already configured in the host Claude CLI session)
and returns a structured ExternalFindings report the synthesiser can weave
into the answer.

We spawn this alongside repo sub-agents, not inside them — that way:
  - Repo agents stay focused on source code (fast, cheap).
  - External fetches don't multiply per repo (one pass for the whole q).
  - Latency of Jira/Confluence round-trips doesn't block code research.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from bifrost.actions._prompt_safety import BOUNDARY_SENTENCE
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.extraction.cli_client import (
    ClaudeCliError,
    ClaudeLlmClient,
    Usage,
)

logger = logging.getLogger(__name__)


@dataclass
class ExternalFinding:
    """One doc/ticket found in Confluence/Jira/etc."""

    source: str  # "confluence" | "jira" | "other"
    title: str
    url: str
    summary: str  # 1-3 sentence summary of why it's relevant


@dataclass
class ExternalFindings:
    """Bundle of external-source hits for one question."""

    query: str
    findings: list[ExternalFinding] = field(default_factory=list)
    searched_sources: list[str] = field(default_factory=list)
    error: str | None = None
    usage: Usage | None = None


_EXTERNAL_SYSTEM_PROMPT = """\
You are a research agent who pulls engineering context from Confluence
and Jira to supplement source-code research for the given question.

You have access to the Atlassian MCP tools. Search BOTH Confluence (for
runbooks, architecture docs, onboarding pages, integration guides) AND
Jira (for recent epics, integration tickets, incident post-mortems) that
match the question's topic.

Constraints:
  - Return AT MOST 8 findings total across both sources.
  - Prefer canonical "how to" / "architecture" pages over chat-thread
    captures and one-off tickets.
  - If nothing directly relevant exists, return an empty findings list
    rather than stretching to loosely-related hits.
  - For each finding, write a 1-2 sentence `summary` explaining what the
    page/ticket contributes to answering the user's question.

__BIFROST_BOUNDARY_SENTINEL__

Respond with ONLY the structured JSON matching the provided schema.
""".replace("__BIFROST_BOUNDARY_SENTINEL__", BOUNDARY_SENTENCE)


_EXTERNAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["source", "title", "url", "summary"],
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["confluence", "jira", "other"],
                    },
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        },
        "searched_sources": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}


# Atlassian MCP tool names we grant the external agent. Scoped list so
# we don't expose write tools (createJiraIssue etc.) which aren't needed
# for read-only research.
_ATLASSIAN_READONLY_TOOL_NAMES = (
    "search",
    "searchConfluenceUsingCql",
    "searchJiraIssuesUsingJql",
    "getConfluencePage",
    "getConfluenceSpaces",
    "getJiraIssue",
    "getJiraIssueRemoteIssueLinks",
    "getPagesInConfluenceSpace",
    "atlassianUserInfo",
    "getAccessibleAtlassianResources",
)


# MCP config that inherits the user's ~/.claude/mcp config. We can't just
# pass an opaque dict here — the host Claude CLI reads its own config when
# --setting-sources is empty, so we selectively enable what we need.
#
# Implementation note: empirically, passing `--strict-mcp-config` with an
# explicit config that contains the atlassian server works IF the atlassian
# server is also running at the user level. Easier path: DON'T pass
# --strict-mcp-config and DON'T override --mcp-config, so the inherited
# one is used. We control which of its tools the agent can call via
# --allowed-tools.
#
# That said, we still want to isolate — so we pass --mcp-config with
# only the atlassian block copied from the host. If it's missing, the
# external agent will fail gracefully and we'll skip external findings.


async def research_external(
    *,
    question: str,
    settings: BifrostSettings | None = None,
) -> ExternalFindings:
    """Spawn one sub-agent that queries Confluence + Jira for question context.

    Never raises — on failure (MCP not configured, auth issue, rate limit)
    returns ExternalFindings(error=..., findings=[]) and the coordinator
    synthesises with code-only data.
    """
    settings = settings or get_settings()

    user_message = (
        f"---SYSTEM-BOUNDARY---\n"
        f"User question:\n{question}\n\n"
        f"Search Confluence (runbooks, architecture, how-to pages) and Jira "
        f"(epics, integration tickets, post-mortems) for documents that help "
        f"answer this question. Return structured findings."
    )

    client = ClaudeLlmClient(settings)
    try:
        completion = await client.call_with_json_schema(
            system_prompt=_EXTERNAL_SYSTEM_PROMPT,
            user_message=user_message,
            model=settings.agent_model,
            json_schema=_EXTERNAL_SCHEMA,
            max_turns=8,  # MCP round-trips are slower than filesystem
            timeout_override=180,  # 90s was too tight — Confluence CQL + Jira JQL can stack
            allowed_tools=settings.mcp.tools("atlassian", _ATLASSIAN_READONLY_TOOL_NAMES),
            # No add_dirs — this agent doesn't touch the filesystem.
            inherit_user_mcp=True,  # Atlassian MCP from user's claude config
        )
    except ClaudeCliError as exc:
        logger.info("External research skipped (%s)", exc)
        return ExternalFindings(
            query=question,
            error=str(exc),
            searched_sources=[],
        )

    data = completion.parsed or {}
    findings = []
    for row in data.get("findings") or []:
        if not isinstance(row, dict):
            continue
        findings.append(
            ExternalFinding(
                source=str(row.get("source", "other")),
                title=str(row.get("title", "")),
                url=str(row.get("url", "")),
                summary=str(row.get("summary", "")),
            )
        )

    return ExternalFindings(
        query=question,
        findings=findings,
        searched_sources=list(data.get("searched_sources") or []),
        usage=completion.usage,
    )
