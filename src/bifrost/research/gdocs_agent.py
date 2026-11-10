"""Per-repo Google Drive / Docs research agent.

Mirrors ``confluence_agent`` and ``slack_agent`` — searches Google
Drive for design docs, architecture write-ups, RFC-style memos, and
historical decision records that help answer the user's question about
ONE specific repo.

Why this exists: in many orgs the load-bearing architectural
documentation lives in Google Docs, not Confluence, so answers that
only see code and Confluence miss the design rationale.

The agent is scoped per-repo via the ``gdoc_folder_url`` field on
``RepoOwnership`` (extension to the existing manifest-derived
ownership row). When set, the agent restricts searches to that folder
+ its descendants. Without scope, the agent uses repo-name keyword
filters and a recent-modified time window so it doesn't drown in
unrelated org docs.

Runs alongside code + Jira + Confluence + GitHub + Slack per-repo
during the coordinator's 2D fan-out.
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
class GDocHit:
    """One Google Doc the agent surfaced for this question."""

    file_id: str  # Drive file id (stable, used for read-back)
    title: str
    url: str  # docs.google.com/document/d/<file_id> form
    mime_type: str  # 'application/vnd.google-apps.document', etc.
    owner_email: str  # owner / first-listed editor (free-form, may be empty)
    last_modified: str  # ISO date or "" if unknown
    summary: str  # 1-2 sentence relevance explanation


@dataclass
class GDocsReport:
    repo_id: str
    repo_name: str
    gdoc_folder_url: str | None  # manifest / RepoOwnership-derived; None when no scope
    relevance: str  # high | medium | low | none | skipped
    summary: str  # 2-4 sentences: what the docs collectively say
    docs: list[GDocHit] = field(default_factory=list)
    searched_queries: list[str] = field(default_factory=list)
    error: str | None = None
    usage: Usage | None = None

    def to_dict(self) -> dict:
        """JSON-serializable form for the agent_results cache."""
        from dataclasses import asdict

        return {
            "repo_id": self.repo_id,
            "repo_name": self.repo_name,
            "gdoc_folder_url": self.gdoc_folder_url,
            "relevance": self.relevance,
            "summary": self.summary,
            "docs": [asdict(d) for d in self.docs],
            "searched_queries": list(self.searched_queries),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> GDocsReport:
        return cls(
            repo_id=d["repo_id"],
            repo_name=d["repo_name"],
            gdoc_folder_url=d.get("gdoc_folder_url"),
            relevance=d.get("relevance", "none"),
            summary=d.get("summary", ""),
            docs=[GDocHit(**p) for p in d.get("docs", [])],
            searched_queries=list(d.get("searched_queries", [])),
            error=d.get("error"),
        )


# ── prompt + schema ──────────────────────────────────────────────────────────


_GDOCS_SYSTEM_PROMPT = """\
You are a Google Drive research agent for ONE specific repository.

Your job: find Google Docs (architecture write-ups, design docs, RFCs,
post-mortems, onboarding docs, decision records) that help answer the
user's question about THIS repo. Older orgs keep load-bearing
architecture in Drive, not Confluence — so the agent searches Drive
specifically for that signal.

You have Google Drive MCP tools:
  - {mcp:gdrive:search_files} (PRIMARY)
  - {mcp:gdrive:read_file_content} (read a doc when a
    snippet looks load-bearing — limit to 2-3 docs per turn)
  - {mcp:gdrive:get_file_metadata} (cheap metadata-only
    probe when you need owner / mtime without reading the body)
  - {mcp:gdrive:list_recent_files} (when scope is open)

## Search-query syntax (Drive's structured query format)

Combine clauses with `and` / `or` / `not` / parentheses. String values
single-quoted. Common terms:

  * `title contains 'X'` — title substring (most precise)
  * `fullText contains 'X'` — title OR body match
  * `mimeType = 'application/vnd.google-apps.document'` — restrict to
    Docs (NOT spreadsheets / slides) — almost always what you want
  * `parentId = '<folder_id>'` — restrict to a specific folder; use
    when the repo has a `gdoc_folder_url` on record
  * `modifiedTime > 'YYYY-MM-DD'` — recent-only filter
  * `owner = 'me'` / `'<email>'` — owner filter (rarely useful here)

## Strategy

  1. **If the repo has a `gdoc_folder_url`**, extract the folder_id
     from the URL and start with:
       `parentId = '<folder_id>' and mimeType =
        'application/vnd.google-apps.document' and fullText contains
        '<keywords>'`
  2. **Otherwise** — the repo has no Drive scope on record — use the
     repo NAME as a primary keyword AND restrict by mimeType:
       `mimeType = 'application/vnd.google-apps.document' and
        fullText contains '<repo-name> <keywords>' and modifiedTime >
        '<2-years-ago>'`
     Two-year cutoff prevents drowning in stale org docs.
  3. **Cap at 6-10 docs per question.** Prefer:
       * Titles like "Design", "Architecture", "RFC", "Decision",
         "Tech Spec", "Onboarding", "Postmortem", "Runbook"
       * Recently-modified docs over stale ones
       * Docs whose owner email matches the repo's squad / domain
  4. For each hit, write a 1-2 sentence summary of how the doc
     answers the question (read the body via
     ``read_file_content`` when the title is ambiguous and the doc
     looks important).

## Constraints

  * Do NOT use ``read_file_content`` more than 2-3 times per turn —
    it pulls the full doc body and pushes hard against context.
    Prefer the search snippet metadata.
  * Do NOT return every doc that matches — targeted, bounded
    results only.
  * If nothing relevant exists, return relevance="none" with empty
    docs and a 1-line summary noting the search was clean.
  * NEVER follow links in doc bodies to other docs — that's the
    org-wide external agent's job.

__BIFROST_BOUNDARY_SENTINEL__

Respond with ONLY the structured JSON schema provided.
""".replace("__BIFROST_BOUNDARY_SENTINEL__", BOUNDARY_SENTENCE)


_GDOCS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["relevance", "summary", "docs"],
    "properties": {
        "relevance": {
            "type": "string",
            "enum": ["high", "medium", "low", "none"],
        },
        "summary": {"type": "string"},
        "docs": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["file_id", "title", "url", "summary"],
                "properties": {
                    "file_id": {"type": "string"},
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "mime_type": {"type": "string"},
                    "owner_email": {"type": "string"},
                    "last_modified": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        },
        "searched_queries": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}


_GDOCS_READ_TOOL_NAMES = (
    "search_files",
    "read_file_content",
    "get_file_metadata",
    "list_recent_files",
    "download_file_content",
    "get_file_permissions",
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _folder_id_from_url(url: str | None) -> str | None:
    """Extract a folder id from common gdoc folder URL forms.

    ``https://drive.google.com/drive/folders/<id>`` →  ``<id>``
    ``https://drive.google.com/drive/u/0/folders/<id>?usp=...`` → ``<id>``
    Anything else → ``None`` (caller falls back to keyword scope).
    """
    if not url:
        return None
    needle = "/folders/"
    idx = url.find(needle)
    if idx == -1:
        return None
    tail = url[idx + len(needle) :]
    fid = tail.split("/", 1)[0].split("?", 1)[0].strip()
    return fid or None


# ── main entrypoint ──────────────────────────────────────────────────────────


async def research_gdocs_for_repo(
    *,
    question: str,
    repo_id: str,
    repo_name: str,
    gdoc_folder_url: str | None,
    squad: str | None = None,
    prior_turns: str = "",
    settings: BifrostSettings | None = None,
    cached_docs: list[dict] | None = None,
) -> GDocsReport:
    """Per-repo Google Drive search.

    ``gdoc_folder_url`` is read from RepoOwnership (set by the
    manifest extension or the offline ``gdocs_index`` mapping pass).
    When ``None``, the agent falls back to repo-name keyword scope —
    less precise but still useful for org-wide design docs that
    mention the repo by name.
    """
    settings = settings or get_settings()

    # prior_turns may itself echo prompt-injected doc bodies; wrap.
    prior_section = (
        "## Prior conversation (for context)\n"
        + wrap_untrusted(prior_turns, "conversation_turn")
        + "\n\n"
        if prior_turns
        else ""
    )

    # Inject cache when we've indexed docs for this repo. Same keyword-
    # scoring trick as the Confluence agent.
    cache_section = ""
    if cached_docs:
        from bifrost.research.jira_agent import (
            _filter_cached_issues as _score,
        )
        from bifrost.research.jira_agent import (
            _question_keywords,
        )

        relevant = _score(cached_docs, _question_keywords(question), limit=40)
        if relevant:
            wrapped = []
            for d in relevant:
                body = f"{d.get('title', '')}\n{d.get('summary', '')[:200]}\n{d.get('url', '')}"
                wrapped.append(
                    wrap_untrusted(
                        body,
                        "gdoc",
                        attrs={"title": str(d.get("title") or "")[:80]},
                    )
                )
            cache_section = (
                "## Pre-indexed Google Docs (cache)\n"
                "Use these as primary source. Live Drive search only if "
                "clearly needed.\n\n" + "\n".join(wrapped) + "\n\n"
            )

    folder_id = _folder_id_from_url(gdoc_folder_url)
    scope_section = (
        f"Drive folder scope: {gdoc_folder_url} (folder_id: {folder_id})"
        if folder_id
        else "Drive folder scope: NONE on record — use repo-name keyword fallback"
    )

    user_message = (
        f"---SYSTEM-BOUNDARY---\n"
        f"{prior_section}"
        f"## Current user question\n"
        f"{wrap_untrusted(question, 'user_intent')}\n\n"
        f"Repository: {repo_name}\n"
        f"{scope_section}\n"
        f"Squad: {squad or '(unknown)'}\n\n"
        f"{cache_section}"
        f"Search Google Drive for design docs / RFCs / decision "
        f"records / post-mortems that help answer this question. "
        f"Cap at 6-10 docs."
    )

    client = ClaudeLlmClient(settings)
    try:
        completion = await client.call_with_dynamic_max_turns(
            initial_max_turns=18,
            timeout_override=240,
            system_prompt=settings.mcp.render(_GDOCS_SYSTEM_PROMPT),
            user_message=user_message,
            model=settings.agent_model,
            json_schema=_GDOCS_SCHEMA,
            allowed_tools=settings.mcp.tools("gdrive", _GDOCS_READ_TOOL_NAMES),
            inherit_user_mcp=True,
        )
    except ClaudeCliError as exc:
        logger.info(
            "GDocs agent for repo %s (folder %s) failed: %s",
            repo_name,
            gdoc_folder_url,
            exc,
        )
        return GDocsReport(
            repo_id=repo_id,
            repo_name=repo_name,
            gdoc_folder_url=gdoc_folder_url,
            relevance="none",
            summary="",
            error=str(exc)[:200],
        )

    data = completion.parsed or {}
    docs: list[GDocHit] = []
    for row in data.get("docs") or []:
        if not isinstance(row, dict):
            continue
        docs.append(
            GDocHit(
                file_id=str(row.get("file_id", "")),
                title=str(row.get("title", "")),
                url=str(row.get("url", "")),
                mime_type=str(
                    row.get(
                        "mime_type",
                        "application/vnd.google-apps.document",
                    )
                ),
                owner_email=str(row.get("owner_email", "")),
                last_modified=str(row.get("last_modified", "")),
                summary=str(row.get("summary", "")),
            ),
        )

    return GDocsReport(
        repo_id=repo_id,
        repo_name=repo_name,
        gdoc_folder_url=gdoc_folder_url,
        relevance=str(data.get("relevance", "none")),
        summary=str(data.get("summary", "")),
        docs=docs,
        searched_queries=list(data.get("searched_queries") or []),
        usage=completion.usage,
    )
