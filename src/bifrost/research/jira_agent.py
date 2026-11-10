"""Per-repo Jira research agent.

Spawns a `claude -p` subprocess with the Atlassian MCP's Jira-read tools,
scoped to THIS repo's Jira project (from the ownership manifest's squad → jira_project_key).

Returns a structured JiraReport with epics, issues, incidents, and any
post-mortem references that match the user's question for this repo.

Parallels RepoResearchAgent — called by the coordinator's 2D fan-out
alongside the code agent + other per-repo agents.
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
class JiraIssueHit:
    """One Jira issue that the agent considers relevant."""

    key: str  # e.g. "SHIP-1234"
    title: str
    status: str  # Open / In Progress / Done / ...
    issue_type: str  # Epic / Story / Bug / Incident / ...
    url: str
    summary: str  # 1-2 sentence relevance explanation from the agent


@dataclass
class JiraReport:
    repo_id: str
    repo_name: str
    jira_project_key: str | None
    relevance: str  # high | medium | low | none | skipped
    summary: str  # 2-4 sentences: what Jira tells us
    issues: list[JiraIssueHit] = field(default_factory=list)
    searched_jql: list[str] = field(default_factory=list)
    error: str | None = None
    usage: Usage | None = None

    def to_dict(self) -> dict:
        """JSON-serializable form for the agent_results cache."""
        from dataclasses import asdict

        return {
            "repo_id": self.repo_id,
            "repo_name": self.repo_name,
            "jira_project_key": self.jira_project_key,
            "relevance": self.relevance,
            "summary": self.summary,
            "issues": [asdict(i) for i in self.issues],
            "searched_jql": list(self.searched_jql),
            "error": self.error,
            # ``usage`` is dropped — replay-from-cache means no new
            # cost this turn, and resurrecting Usage from JSON adds
            # complexity for no caller value.
        }

    @classmethod
    def from_dict(cls, d: dict) -> JiraReport:
        return cls(
            repo_id=d["repo_id"],
            repo_name=d["repo_name"],
            jira_project_key=d.get("jira_project_key"),
            relevance=d.get("relevance", "none"),
            summary=d.get("summary", ""),
            issues=[JiraIssueHit(**i) for i in d.get("issues", [])],
            searched_jql=list(d.get("searched_jql", [])),
            error=d.get("error"),
        )


_JIRA_SYSTEM_PROMPT = """\
You are a Jira research agent for ONE specific repository.

Your job: find Jira issues (epics, stories, bugs, incidents, post-mortems)
that help answer the user's question for THIS repo.

You have Atlassian MCP tools:
  - {mcp:atlassian:searchJiraIssuesUsingJql} (PRIMARY)
  - {mcp:atlassian:getJiraIssue}

Strategy (in order — give up on a step only if it yields nothing):
  1. Try the provided `jira_project_key` with keyword filters:
       `project = "<KEY>" AND text ~ "<question keywords>"`
  2. If step 1 returns 0 hits, broaden the TEXT QUERY but KEEP the
     project filter — the user explicitly scoped this query to a
     specific repo, so a company-wide search violates that scope.
     Try in order:
       a. `project = "<KEY>" AND (text ~ "<repo_name>" OR component = "<repo_name>")`
       b. `project = "<KEY>" AND issuetype in (Epic, Incident, Bug) AND text ~ "<broader keywords>"`
       c. If `<KEY>` is plausibly wrong (squad-derived heuristic),
          ask the operator to widen scope rather than scanning every
          project. Return `relevance: "none"` with a note in `summary`
          indicating "no matches in project <KEY>; consider scoping
          the question to a different team/project". The org-wide
          external agent (separate from this per-repo agent) handles
          the cross-project case — DO NOT duplicate it here.
  3. For epics/post-mortems/incidents, filter by `issuetype in (Epic, Incident)`
     OR `labels in (post-mortem, incident, outage)` — still scoped to `<KEY>`.
  4. Cap at 6-10 relevant issues. Prefer recent + high-level over sprawl.
  5. For each hit, write a 1-2 sentence summary of relevance.

Constraints:
  - Bounded searches only — max ~4 JQL queries total.
  - EVERY JQL query MUST include `project = "<KEY>"` (or
    `project in (<KEY>, <linked-keys>)` only if you confirmed the
    linked keys via a `getJiraIssueRemoteIssueLinks` call). NO
    unscoped `text ~ "..."` queries. The cross-project search lives
    in the org-wide external agent, not here.
  - If truly nothing is relevant in `<KEY>`, return `relevance: "none"`
    and note in `summary` whether the scope was likely wrong — so the
    user knows to broaden manually rather than us silently doing it.
  - Focus on epics + post-mortems + incident tickets — those carry intent.

__BIFROST_BOUNDARY_SENTINEL__

Respond with ONLY the structured JSON schema provided.
""".replace("__BIFROST_BOUNDARY_SENTINEL__", BOUNDARY_SENTENCE)


_JIRA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["relevance", "summary", "issues"],
    "properties": {
        "relevance": {
            "type": "string",
            "enum": ["high", "medium", "low", "none"],
        },
        "summary": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["key", "title", "url", "summary"],
                "properties": {
                    "key": {"type": "string"},
                    "title": {"type": "string"},
                    "status": {"type": "string"},
                    "issue_type": {"type": "string"},
                    "url": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        },
        "searched_jql": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}


_JIRA_READ_TOOL_NAMES = (
    "search",
    "searchJiraIssuesUsingJql",
    "getJiraIssue",
    "getJiraIssueRemoteIssueLinks",
    "getTransitionsForJiraIssue",
    "getVisibleJiraProjects",
    "getIssueLinkTypes",
    "atlassianUserInfo",
    "getAccessibleAtlassianResources",
)


async def research_jira_for_repo(
    *,
    question: str,
    repo_id: str,
    repo_name: str,
    jira_project_key: str | None,
    squad: str | None = None,
    prior_turns: str = "",
    settings: BifrostSettings | None = None,
    cached_issues: list[dict] | None = None,
) -> JiraReport:
    """Per-repo Jira research. Caller should have resolved jira_project_key
    from ownership metadata; if missing, we skip (no Jira signal) rather
    than flailing across the whole org."""
    settings = settings or get_settings()

    if not jira_project_key:
        return JiraReport(
            repo_id=repo_id,
            repo_name=repo_name,
            jira_project_key=None,
            relevance="skipped",
            summary="No Jira project key on record (ownership manifest missing or squad unknown).",
        )

    # prior_turns contains assistant + user history that may
    # itself echo prompt-injected Jira ticket bodies; wrap it.
    prior_section = (
        "## Prior conversation (for context)\n"
        + wrap_untrusted(prior_turns, "conversation_turn")
        + "\n\n"
        if prior_turns
        else ""
    )

    # Cached index — if we pre-indexed this project, embed the top-N
    # recently-updated / keyword-matching issues so the agent can pick
    # relevant ones WITHOUT making live MCP calls. This is the big
    # cost-saving path: from ~$0.50/call down to near-zero.
    # Each cached issue body is attacker-controlled (a Jira ticket
    # author can put anything in description/summary). Wrap each one in
    # a <jira_ticket key="..."> boundary.
    cache_section = ""
    if cached_issues:
        kws = _question_keywords(question)
        relevant = _filter_cached_issues(cached_issues, kws, limit=40)
        if relevant:
            wrapped = []
            for c in relevant:
                body = (
                    f"[{c.get('status', '?')}/{c.get('issue_type', '?')}]: "
                    f"{c.get('title', '')}\n"
                    f"{c.get('summary', '')[:200]}\n"
                    f"{c.get('url', '')}"
                )
                wrapped.append(
                    wrap_untrusted(
                        body,
                        "jira_ticket",
                        attrs={"key": str(c.get("issue_key") or "?")},
                    )
                )
            cache_section = (
                "## Pre-indexed Jira issues (cache, last synced recently)\n"
                "Use these as primary source. Only call live Jira tools "
                "if the cache is clearly missing relevant tickets.\n\n"
                + "\n".join(wrapped)
                + "\n\n"
            )

    user_message = (
        f"---SYSTEM-BOUNDARY---\n"
        f"{prior_section}"
        f"## Current user question\n"
        f"{wrap_untrusted(question, 'user_intent')}\n\n"
        f"Repository: {repo_name}\n"
        f"Jira project: {jira_project_key}\n"
        f"Squad: {squad or '(unknown)'}\n\n"
        f"{cache_section}"
        f"Search Jira for issues in project {jira_project_key} that help "
        f"answer this question. Use JQL filters on `project = {jira_project_key}` "
        f"plus keyword or label filters from the question. Focus on epics + "
        f"incidents + post-mortems. Cap at 6-10 issues total."
    )

    client = ClaudeLlmClient(settings)
    try:
        completion = await client.call_with_json_schema(
            system_prompt=settings.mcp.render(_JIRA_SYSTEM_PROMPT),
            user_message=user_message,
            model=settings.agent_model,
            json_schema=_JIRA_SCHEMA,
            max_turns=12,  # MCP queries + emit; 8 was leaving no room
            timeout_override=180,
            allowed_tools=settings.mcp.tools("atlassian", _JIRA_READ_TOOL_NAMES),
            inherit_user_mcp=True,
        )
    except ClaudeCliError as exc:
        logger.info(
            "Jira agent for repo %s (project %s) failed: %s",
            repo_name,
            jira_project_key,
            exc,
        )
        return JiraReport(
            repo_id=repo_id,
            repo_name=repo_name,
            jira_project_key=jira_project_key,
            relevance="none",
            summary="",
            error=str(exc)[:200],
        )

    data = completion.parsed or {}
    issues = []
    for row in data.get("issues") or []:
        if not isinstance(row, dict):
            continue
        issues.append(
            JiraIssueHit(
                key=str(row.get("key", "")),
                title=str(row.get("title", "")),
                status=str(row.get("status", "")),
                issue_type=str(row.get("issue_type", "")),
                url=str(row.get("url", "")),
                summary=str(row.get("summary", "")),
            )
        )

    return JiraReport(
        repo_id=repo_id,
        repo_name=repo_name,
        jira_project_key=jira_project_key,
        relevance=str(data.get("relevance", "none")),
        summary=str(data.get("summary", "")),
        issues=issues,
        searched_jql=list(data.get("searched_jql") or []),
        usage=completion.usage,
    )


# ── Cache helpers ─────────────────────────────────────────────────


def _question_keywords(q: str) -> list[str]:
    """Split question into keyword tokens for cache filtering."""
    import re

    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]+", q.lower())
    stop = {
        "the",
        "and",
        "for",
        "with",
        "how",
        "what",
        "does",
        "this",
        "that",
        "are",
        "from",
        "into",
        "when",
        "where",
        "which",
        "why",
    }
    return [t for t in tokens if len(t) > 2 and t not in stop]


def _filter_cached_issues(
    cache: list[dict],
    keywords: list[str],
    *,
    limit: int = 40,
) -> list[dict]:
    """Score cached issues against keywords. Higher hit-count first."""
    if not keywords:
        return list(cache)[:limit]

    scored: list[tuple[int, dict]] = []
    for row in cache:
        hay = (
            f"{row.get('title', '')} "
            f"{row.get('summary', '')} "
            f"{row.get('description_excerpt', '')} "
            f"{' '.join(row.get('labels') or [])} "
            f"{' '.join(row.get('components') or [])}"
        ).lower()
        score = sum(1 for kw in keywords if kw in hay)
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]
