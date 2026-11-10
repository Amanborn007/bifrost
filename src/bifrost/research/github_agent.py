"""Per-repo GitHub research agent — recent PRs, issues, commits.

Complements CodeAgent (static source) with *activity* signal: what's
been merged recently, what's being discussed in PR reviews, which
issues are open. This is high-signal for questions like "recent changes
to retry logic" or "known bugs".

Uses the authenticated github MCP (it must show as connected).
If the MCP is absent or returns nothing, we return a graceful empty
report — code + Jira + Confluence still synthesize without us.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from bifrost.actions._prompt_safety import BOUNDARY_SENTENCE
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient, Usage

logger = logging.getLogger(__name__)


@dataclass
class PRHit:
    number: int
    title: str
    state: str  # open / closed / merged
    url: str
    summary: str  # 1-2 sentence relevance


@dataclass
class IssueHit:
    number: int
    title: str
    state: str
    url: str
    summary: str


@dataclass
class GitHubReport:
    repo_id: str
    repo_name: str
    github_owner: str | None
    github_name: str | None
    relevance: str  # high | medium | low | none | skipped
    summary: str  # 2-4 sentences
    recent_prs: list[PRHit] = field(default_factory=list)
    relevant_issues: list[IssueHit] = field(default_factory=list)
    error: str | None = None
    usage: Usage | None = None

    def to_dict(self) -> dict:
        """JSON-serialisable form for the agent_results cache."""
        from dataclasses import asdict

        return {
            "repo_id": self.repo_id,
            "repo_name": self.repo_name,
            "github_owner": self.github_owner,
            "github_name": self.github_name,
            "relevance": self.relevance,
            "summary": self.summary,
            "recent_prs": [asdict(p) for p in self.recent_prs],
            "relevant_issues": [asdict(i) for i in self.relevant_issues],
            "error": self.error,
            # ``usage`` dropped — replay-from-cache pays no new cost
        }

    @classmethod
    def from_dict(cls, d: dict) -> GitHubReport:
        return cls(
            repo_id=d["repo_id"],
            repo_name=d["repo_name"],
            github_owner=d.get("github_owner"),
            github_name=d.get("github_name"),
            relevance=d.get("relevance", "none"),
            summary=d.get("summary", ""),
            recent_prs=[PRHit(**p) for p in d.get("recent_prs", [])],
            relevant_issues=[IssueHit(**i) for i in d.get("relevant_issues", [])],
            error=d.get("error"),
        )


_GITHUB_SYSTEM_PROMPT = """\
You are a GitHub activity research agent for ONE specific repository.

Your job: surface the RECENT and RELEVANT PRs, issues, and discussions
that answer the user's question. Activity signal complements static code
analysis — things like "what changed recently" and "what bugs are open"
only live on GitHub.

You have github MCP tools:
  - {mcp:github:list_pull_requests}         (PRs by state, head, base, labels)
  - {mcp:github:get_pull_request}           (details — description, reviews)
  - {mcp:github:list_issues}                (issues by state, label, since)
  - {mcp:github:get_issue}                  (full issue body + comments)
  - {mcp:github:search_code}                (in-repo code search)
  - {mcp:github:search_issues}              (cross-repo text search)
  - {mcp:github:list_commits}               (recent commits)

Strategy:
  1. list_pull_requests(state="closed", per_page=10, sort="updated")
     to see what shipped recently.
  2. search_issues in this repo for keywords from the question, filtering
     to is:open or is:closed within the last ~6 months.
  3. For the 3-5 most relevant PRs/issues, get details.
  4. Cap total at ~8-10 items combined. Don't be exhaustive.

Constraints:
  - Stay scoped to owner/name provided. Do not wander into other repos.
  - Summaries per item: 1-2 sentences explaining relevance.
  - If nothing relevant exists, return relevance="none" with empty lists.

__BIFROST_BOUNDARY_SENTINEL__

Respond with ONLY the structured JSON schema provided.
""".replace("__BIFROST_BOUNDARY_SENTINEL__", BOUNDARY_SENTENCE)


_GITHUB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["relevance", "summary", "recent_prs", "relevant_issues"],
    "properties": {
        "relevance": {
            "type": "string",
            "enum": ["high", "medium", "low", "none"],
        },
        "summary": {"type": "string"},
        "recent_prs": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["number", "title", "url", "summary"],
                "properties": {
                    "number": {"type": "integer"},
                    "title": {"type": "string"},
                    "state": {"type": "string"},
                    "url": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        },
        "relevant_issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["number", "title", "url", "summary"],
                "properties": {
                    "number": {"type": "integer"},
                    "title": {"type": "string"},
                    "state": {"type": "string"},
                    "url": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        },
    },
}


_GITHUB_READ_TOOL_NAMES = (
    "list_pull_requests",
    "get_pull_request",
    "get_pull_request_comments",
    "get_pull_request_reviews",
    "get_pull_request_files",
    "get_pull_request_status",
    "list_issues",
    "get_issue",
    "search_code",
    "search_issues",
    "list_commits",
)


def _parse_github_url(url: str) -> tuple[str, str] | None:
    """Return (owner, name) from an http(s) GitHub URL, or None."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if "github.com" not in parsed.netloc:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        return None
    owner, name = parts[0], parts[1]
    if name.endswith(".git"):
        name = name[:-4]
    return owner, name


async def research_github_for_repo(
    *,
    question: str,
    repo_id: str,
    repo_name: str,
    repo_url: str,
    prior_turns: str = "",
    settings: BifrostSettings | None = None,
) -> GitHubReport:
    """Per-repo GitHub activity research. Skips gracefully if URL isn't github.com."""
    settings = settings or get_settings()

    gh = _parse_github_url(repo_url)
    if gh is None:
        return GitHubReport(
            repo_id=repo_id,
            repo_name=repo_name,
            github_owner=None,
            github_name=None,
            relevance="skipped",
            summary="Not a GitHub URL — no recent-activity signal available.",
        )
    owner, name = gh

    prior_section = f"## Prior conversation (for context)\n{prior_turns}\n\n" if prior_turns else ""
    user_message = (
        f"---SYSTEM-BOUNDARY---\n"
        f"{prior_section}"
        f"## Current user question\n{question}\n\n"
        f"GitHub repo: {owner}/{name}\n"
        f"(pass this exact owner + name to every github MCP tool)\n\n"
        f"Find recent PRs (last ~90 days) and open/closed issues that help "
        f"answer the question. Cap at 8-10 items total across both."
    )

    client = ClaudeLlmClient(settings)
    try:
        # Dynamic budget — start at 18 turns / 240s, auto-bump to
        # ~28 turns / ~390s on max_turns. Retry is cache-warm.
        completion = await client.call_with_dynamic_max_turns(
            initial_max_turns=18,
            timeout_override=240,
            system_prompt=settings.mcp.render(_GITHUB_SYSTEM_PROMPT),
            user_message=user_message,
            model=settings.agent_model,
            json_schema=_GITHUB_SCHEMA,
            allowed_tools=settings.mcp.tools("github", _GITHUB_READ_TOOL_NAMES),
            inherit_user_mcp=True,
        )
    except ClaudeCliError as exc:
        logger.info(
            "GitHub agent for repo %s (%s/%s) failed: %s",
            repo_name,
            owner,
            name,
            exc,
        )
        return GitHubReport(
            repo_id=repo_id,
            repo_name=repo_name,
            github_owner=owner,
            github_name=name,
            relevance="none",
            summary="",
            error=str(exc)[:200],
        )

    data = completion.parsed or {}
    prs = []
    for row in data.get("recent_prs") or []:
        if not isinstance(row, dict):
            continue
        try:
            prs.append(
                PRHit(
                    number=int(row.get("number", 0) or 0),
                    title=str(row.get("title", "")),
                    state=str(row.get("state", "")),
                    url=str(row.get("url", "")),
                    summary=str(row.get("summary", "")),
                ),
            )
        except (TypeError, ValueError):
            continue

    issues = []
    for row in data.get("relevant_issues") or []:
        if not isinstance(row, dict):
            continue
        try:
            issues.append(
                IssueHit(
                    number=int(row.get("number", 0) or 0),
                    title=str(row.get("title", "")),
                    state=str(row.get("state", "")),
                    url=str(row.get("url", "")),
                    summary=str(row.get("summary", "")),
                ),
            )
        except (TypeError, ValueError):
            continue

    return GitHubReport(
        repo_id=repo_id,
        repo_name=repo_name,
        github_owner=owner,
        github_name=name,
        relevance=str(data.get("relevance", "none")),
        summary=str(data.get("summary", "")),
        recent_prs=prs,
        relevant_issues=issues,
        usage=completion.usage,
    )
