"""Per-repo Confluence research agent.

Searches the repo's Confluence space (from the ownership manifest's squad →
confluence_space_key) for runbooks, architecture pages, onboarding docs,
and post-mortems matching the user's question.

Parallels the Jira agent — runs alongside code + Jira per-repo during
the coordinator's 2D fan-out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from bifrost.actions._prompt_safety import BOUNDARY_SENTENCE, wrap_untrusted
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient, Usage

logger = logging.getLogger(__name__)


@dataclass
class ConfluencePageHit:
    id: str
    title: str
    url: str
    space_key: str
    last_modified: str  # ISO string if known, else ""
    summary: str  # 1-2 sentence relevance explanation


@dataclass
class ConfluenceReport:
    repo_id: str
    repo_name: str
    confluence_space_key: str | None
    relevance: str  # high | medium | low | none | skipped
    summary: str  # 2-4 sentences: what the docs say
    pages: list[ConfluencePageHit] = field(default_factory=list)
    searched_cql: list[str] = field(default_factory=list)
    error: str | None = None
    usage: Usage | None = None

    def to_dict(self) -> dict:
        """JSON-serializable form for the agent_results cache."""
        from dataclasses import asdict

        return {
            "repo_id": self.repo_id,
            "repo_name": self.repo_name,
            "confluence_space_key": self.confluence_space_key,
            "relevance": self.relevance,
            "summary": self.summary,
            "pages": [asdict(p) for p in self.pages],
            "searched_cql": list(self.searched_cql),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ConfluenceReport:
        return cls(
            repo_id=d["repo_id"],
            repo_name=d["repo_name"],
            confluence_space_key=d.get("confluence_space_key"),
            relevance=d.get("relevance", "none"),
            summary=d.get("summary", ""),
            pages=[ConfluencePageHit(**p) for p in d.get("pages", [])],
            searched_cql=list(d.get("searched_cql", [])),
            error=d.get("error"),
        )


_CONFLUENCE_SYSTEM_PROMPT = """\
You are a Confluence research agent for ONE specific repository.

Your job: find Confluence pages (runbooks, architecture docs, onboarding
guides, post-mortems) in THIS repo's space that help answer the user's
question.

You have Atlassian MCP tools:
  - {mcp:atlassian:searchConfluenceUsingCql} (PRIMARY)
  - {mcp:atlassian:getConfluencePage} (to read a full page when a snippet
    looks very relevant)
  - {mcp:atlassian:getConfluenceSpaces} (to confirm space exists)

Strategy:
  1. Start with CQL scoped to the repo's space: `space = "<key>" AND text ~ "..."`.
  2. If the scoped search returns nothing useful, broaden the TEXT QUERY
     but KEEP the space filter — the user explicitly scoped this query
     to a specific repo, so a company-wide search violates that scope.
     Try in order:
       a. `space = "<key>" AND title ~ "..."` (title-only, often more
          precise than text)
       b. `space = "<key>" AND (type = page OR type = blogpost) AND
          text ~ "<broader keywords>"`
       c. If the space genuinely lacks relevant docs, return
          `relevance: "none"` with a `summary` noting that. The
          org-wide external agent (separate from this per-repo
          agent) handles cross-space search — DO NOT duplicate it.
  3. Cap at 6-10 pages. Prefer:
       - Pages titled like "Runbook", "Integration Guide", "Onboarding", "Architecture"
       - Recently-edited pages over stale ones
       - Pages that clearly describe a process/intent, not random meeting notes
  4. For each hit, write a 1-2 sentence summary of how it answers the question.

Constraints:
  - EVERY CQL query MUST include `space = "<key>"`. NO unscoped
    `text ~ "..."` searches. The cross-space search lives in the
    org-wide external agent, not here.
  - Do not return every page — targeted, bounded results only.
  - If nothing relevant exists in `<key>`, return relevance="none"
    with empty pages and a `summary` noting whether the space was
    likely wrong (so the user knows to broaden manually).
  - Use {mcp:atlassian:getConfluencePage} sparingly — only when you need
    to verify a snippet; don't dump whole pages into the conversation.

__BIFROST_BOUNDARY_SENTINEL__

Respond with ONLY the structured JSON schema provided.
""".replace("__BIFROST_BOUNDARY_SENTINEL__", BOUNDARY_SENTENCE)


_CONFLUENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["relevance", "summary", "pages"],
    "properties": {
        "relevance": {
            "type": "string",
            "enum": ["high", "medium", "low", "none"],
        },
        "summary": {"type": "string"},
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "title", "url", "summary"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "space_key": {"type": "string"},
                    "last_modified": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        },
        "searched_cql": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}


_CONFLUENCE_READ_TOOL_NAMES = (
    "search",
    "searchConfluenceUsingCql",
    "getConfluencePage",
    "getConfluencePageDescendants",
    "getConfluencePageFooterComments",
    "getConfluencePageInlineComments",
    "getConfluenceSpaces",
    "getPagesInConfluenceSpace",
    "atlassianUserInfo",
    "getAccessibleAtlassianResources",
)


async def research_confluence_for_repo(
    *,
    question: str,
    repo_id: str,
    repo_name: str,
    confluence_space_key: str | None,
    squad: str | None = None,
    prior_turns: str = "",
    settings: BifrostSettings | None = None,
    cached_pages: list[dict] | None = None,
) -> ConfluenceReport:
    """Per-repo Confluence search."""
    settings = settings or get_settings()

    if not confluence_space_key:
        return ConfluenceReport(
            repo_id=repo_id,
            repo_name=repo_name,
            confluence_space_key=None,
            relevance="skipped",
            summary="No Confluence space key on record (ownership manifest missing or squad unknown).",
        )

    # prior_turns may itself echo prompt-injected page bodies; wrap.
    prior_section = (
        "## Prior conversation (for context)\n"
        + wrap_untrusted(prior_turns, "conversation_turn")
        + "\n\n"
        if prior_turns
        else ""
    )

    # Inject cache if we've indexed this space — same cost-saving trick
    # as the Jira agent. Each cached page body is attacker-
    # controlled (Confluence editors can write anything). Wrap each.
    cache_section = ""
    if cached_pages:
        from bifrost.research.jira_agent import (
            _filter_cached_issues as _score,
        )
        from bifrost.research.jira_agent import (
            _question_keywords,
        )

        relevant = _score(cached_pages, _question_keywords(question), limit=40)
        if relevant:
            wrapped = []
            for p in relevant:
                body = f"{p.get('title', '')}\n{p.get('summary', '')[:200]}\n{p.get('url', '')}"
                wrapped.append(
                    wrap_untrusted(
                        body,
                        "confluence_page",
                        attrs={"title": str(p.get("title") or "")[:80]},
                    )
                )
            cache_section = (
                "## Pre-indexed Confluence pages (cache)\n"
                "Use these as primary source. Live CQL only if clearly needed.\n\n"
                + "\n".join(wrapped)
                + "\n\n"
            )

    user_message = (
        f"---SYSTEM-BOUNDARY---\n"
        f"{prior_section}"
        f"## Current user question\n"
        f"{wrap_untrusted(question, 'user_intent')}\n\n"
        f"Repository: {repo_name}\n"
        f"Confluence space: {confluence_space_key}\n"
        f"Squad: {squad or '(unknown)'}\n\n"
        f"{cache_section}"
        f'Search Confluence for pages in space "{confluence_space_key}" '
        f"that help answer this question. Use CQL, prioritize runbooks / "
        f"architecture / onboarding / post-mortem pages. Cap at 6-10 pages."
    )

    client = ClaudeLlmClient(settings)
    try:
        # Dynamic budget — start at 18 turns / 240s, auto-bump to
        # ~28 turns / ~390s on max_turns. Retry is cache-warm.
        completion = await client.call_with_dynamic_max_turns(
            initial_max_turns=18,
            timeout_override=240,
            system_prompt=settings.mcp.render(_CONFLUENCE_SYSTEM_PROMPT),
            user_message=user_message,
            model=settings.agent_model,
            json_schema=_CONFLUENCE_SCHEMA,
            allowed_tools=settings.mcp.tools("atlassian", _CONFLUENCE_READ_TOOL_NAMES),
            inherit_user_mcp=True,
        )
    except ClaudeCliError as exc:
        logger.info(
            "Confluence agent for repo %s (space %s) failed: %s",
            repo_name,
            confluence_space_key,
            exc,
        )
        return ConfluenceReport(
            repo_id=repo_id,
            repo_name=repo_name,
            confluence_space_key=confluence_space_key,
            relevance="none",
            summary="",
            error=str(exc)[:200],
        )

    data = completion.parsed or {}
    pages = []
    for row in data.get("pages") or []:
        if not isinstance(row, dict):
            continue
        pages.append(
            ConfluencePageHit(
                id=str(row.get("id", "")),
                title=str(row.get("title", "")),
                url=str(row.get("url", "")),
                space_key=str(row.get("space_key", confluence_space_key or "")),
                last_modified=str(row.get("last_modified", "")),
                summary=str(row.get("summary", "")),
            )
        )

    return ConfluenceReport(
        repo_id=repo_id,
        repo_name=repo_name,
        confluence_space_key=confluence_space_key,
        relevance=str(data.get("relevance", "none")),
        summary=str(data.get("summary", "")),
        pages=pages,
        searched_cql=list(data.get("searched_cql") or []),
        usage=completion.usage,
    )
