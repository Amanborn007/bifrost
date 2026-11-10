"""Per-repo Slack research agent.

Searches the repo's Slack channel (from repo_ownership.slack_channel —
squad-derived, e.g. "#ship") for recent threads about the user's question.

Signal surfaced is complementary to code/Jira/Confluence:
  - Recent incidents discussed in channel
  - Ownership conversations ("who owns X?")
  - Decisions that didn't make it to a ticket

Uses the claude.ai Slack MCP search and read tools.
Fails gracefully if auth/MCP is absent — chat still synthesizes without us.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from bifrost.actions._prompt_safety import BOUNDARY_SENTENCE
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient, Usage

logger = logging.getLogger(__name__)


@dataclass
class SlackHit:
    """One message or thread the agent deems relevant."""

    channel: str
    author: str
    ts: str  # slack ts / permalink-ish
    url: str
    excerpt: str  # short quoted body
    summary: str  # 1-2 sentences: why it matters for this q


@dataclass
class SlackReport:
    repo_id: str
    repo_name: str
    slack_channel: str | None
    relevance: str  # high | medium | low | none | skipped
    summary: str  # 2-4 sentences synthesizing the channel's take
    hits: list[SlackHit] = field(default_factory=list)
    error: str | None = None
    usage: Usage | None = None

    def to_dict(self) -> dict:
        """JSON-serialisable form for the agent_results cache."""
        from dataclasses import asdict

        return {
            "repo_id": self.repo_id,
            "repo_name": self.repo_name,
            "slack_channel": self.slack_channel,
            "relevance": self.relevance,
            "summary": self.summary,
            "hits": [asdict(h) for h in self.hits],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SlackReport:
        return cls(
            repo_id=d["repo_id"],
            repo_name=d["repo_name"],
            slack_channel=d.get("slack_channel"),
            relevance=d.get("relevance", "none"),
            summary=d.get("summary", ""),
            hits=[SlackHit(**h) for h in d.get("hits", [])],
            error=d.get("error"),
        )


_SLACK_SYSTEM_PROMPT = """\
You are a Slack research agent for ONE specific repository's channel.

Your job: surface recent channel messages + threads related to the user's
question. Slack captures real-time incident chatter, ownership pings,
decisions, and workarounds that don't always make it into tickets or
code. That's the unique value.

You have claude.ai Slack MCP tools:
  - {mcp:slack:slack_search_public} (PRIMARY — channel-scoped search)
  - {mcp:slack:slack_search_public_and_private}
  - {mcp:slack:slack_read_thread}  (for context on a specific thread)
  - {mcp:slack:slack_read_channel} (recent channel messages)

Strategy:
  1. slack_search_public with a query like `in:<channel> <keywords>`
     derived from the user's question. Keep it 1-2 queries max.
  2. If a specific thread clearly answers the question, read_thread once
     to pull context. Otherwise skip and use the search snippets.
  3. Cap at 6-10 hits total. Prefer threads with reactions or many
     replies — they're canonical. Deprioritize reaction-less singletons.
  4. For each hit, write a 1-2 sentence `summary` explaining relevance
     and a short `excerpt` quoting the key text.

Constraints:
  - Do NOT send or draft messages. Search + read only.
  - If the channel is empty or nothing matches the question, return
    relevance="none" with an empty hits list.
  - MAX 5 total tool calls (search + maybe 1-2 thread reads + emit).

__BIFROST_BOUNDARY_SENTINEL__

Respond with ONLY the structured JSON schema provided.
""".replace("__BIFROST_BOUNDARY_SENTINEL__", BOUNDARY_SENTENCE)


_SLACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["relevance", "summary", "hits"],
    "properties": {
        "relevance": {
            "type": "string",
            "enum": ["high", "medium", "low", "none"],
        },
        "summary": {"type": "string"},
        "hits": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["channel", "author", "url", "summary"],
                "properties": {
                    "channel": {"type": "string"},
                    "author": {"type": "string"},
                    "ts": {"type": "string"},
                    "url": {"type": "string"},
                    "excerpt": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        },
    },
}


_SLACK_READ_TOOL_NAMES = (
    "slack_search_public",
    "slack_search_public_and_private",
    "slack_search_channels",
    "slack_search_users",
    "slack_read_thread",
    "slack_read_channel",
    "slack_read_user_profile",
)


async def research_slack_for_repo(
    *,
    question: str,
    repo_id: str,
    repo_name: str,
    slack_channel: str | None,
    squad: str | None = None,
    prior_turns: str = "",
    settings: BifrostSettings | None = None,
) -> SlackReport:
    """Per-repo Slack channel research.

    If no channel is known for the repo (no ownership record or squad
    derivation failed), skip cleanly.
    """
    settings = settings or get_settings()

    if not slack_channel:
        return SlackReport(
            repo_id=repo_id,
            repo_name=repo_name,
            slack_channel=None,
            relevance="skipped",
            summary="No Slack channel on record (ownership manifest missing or squad unknown).",
        )

    prior_section = f"## Prior conversation (for context)\n{prior_turns}\n\n" if prior_turns else ""
    user_message = (
        f"---SYSTEM-BOUNDARY---\n"
        f"{prior_section}"
        f"## Current user question\n{question}\n\n"
        f"Repository: {repo_name}\n"
        f"Slack channel: {slack_channel}\n"
        f"Squad: {squad or '(unknown)'}\n\n"
        f"Search this channel for recent threads relevant to the question. "
        f"Also consider sibling squad channels if you spot them in results. "
        f"Cap at 6-10 hits."
    )

    client = ClaudeLlmClient(settings)
    try:
        # Dynamic budget: start at 16 turns / 180s, auto-bump to ~26
        # turns / ~290s if Slack's paginated search burns through the
        # initial budget. A cache-warm retry costs far less than paying
        # for a hardcoded 26-turn cap on every call.
        completion = await client.call_with_dynamic_max_turns(
            initial_max_turns=16,
            timeout_override=180,
            system_prompt=settings.mcp.render(_SLACK_SYSTEM_PROMPT),
            user_message=user_message,
            model=settings.agent_model,
            json_schema=_SLACK_SCHEMA,
            allowed_tools=[*settings.mcp.tools("slack", _SLACK_READ_TOOL_NAMES), "Read"],
            inherit_user_mcp=True,
        )
    except ClaudeCliError as exc:
        logger.info(
            "Slack agent for repo %s (%s) failed: %s",
            repo_name,
            slack_channel,
            exc,
        )
        return SlackReport(
            repo_id=repo_id,
            repo_name=repo_name,
            slack_channel=slack_channel,
            relevance="none",
            summary="",
            error=str(exc)[:200],
        )

    data = completion.parsed or {}
    hits = []
    for row in data.get("hits") or []:
        if not isinstance(row, dict):
            continue
        hits.append(
            SlackHit(
                channel=str(row.get("channel", slack_channel or "")),
                author=str(row.get("author", "")),
                ts=str(row.get("ts", "")),
                url=str(row.get("url", "")),
                excerpt=str(row.get("excerpt", "")),
                summary=str(row.get("summary", "")),
            )
        )

    return SlackReport(
        repo_id=repo_id,
        repo_name=repo_name,
        slack_channel=slack_channel,
        relevance=str(data.get("relevance", "none")),
        summary=str(data.get("summary", "")),
        hits=hits,
        usage=completion.usage,
    )
