"""Pre-index Confluence + Jira per repo so chat can retrieve from DB.

Each repo maps to a Confluence space + Jira project (from repo_ownership,
squad-derived + discovery-refined). This module spawns a one-shot
``claude -p`` subprocess that uses Atlassian MCP to enumerate pages and
issues, then upserts them into ``confluence_pages`` and ``jira_issues``.

Chat retrieval (``cached_retrieval.py``) reads from those tables; the
live Jira/Confluence agents become fallbacks for when the cache is empty
or clearly stale. Net effect: typical chat turns get much cheaper
because the heavy MCP exploration happens once per repo, once per day.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.cli.config_sections import JiraSourceConfig
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient
from bifrost.storage.models import (
    ConfluencePage,
    ExternalSyncLog,
    JiraIssue,
    Repo,
    RepoOwnership,
)

logger = logging.getLogger(__name__)


@dataclass
class IndexResult:
    repo_name: str
    source: str  # "confluence" | "jira"
    items_indexed: int
    error: str | None = None


# ── Confluence indexer ─────────────────────────────────────────────


_CONF_INDEX_SYSTEM_PROMPT = """\
You are a BULK Confluence indexer. Minimize tool calls. Your entire job:

  Make exactly TWO CQL calls with SMALL pages (MCP auto-redirects large
  responses to disk and the Read tool rejects files >25K tokens, so we
  MUST keep responses inline):

  Call 1:
     cql: space = "<KEY>" AND type = page ORDER BY lastmodified DESC
     limit: 10

  Call 2 (NEXT PAGE):
     Same CQL, limit: 10, start: 10

  Then emit structured output combining both. Don't Read temp files —
  those indicate the payload was too big. Retry with a smaller limit.

Hard rules:
  - AT MOST 3 total tool calls (2 searches + 1 emit).
  - No getConfluencePage calls.
  - If response 1 is a file path (too big), RETRY with limit=5.

Hard rules — follow exactly:
  - Exactly ONE {mcp:atlassian:searchConfluenceUsingCql} call. No more.
  - Do NOT call {mcp:atlassian:getConfluencePage}. Titles + CQL snippets
    are all we need for a cache.
  - Do NOT refine with additional searches. If the first query returned
    nothing, emit `pages: []` and stop.
  - You may Read ONE file if the MCP result redirected to disk.
  - Emit the StructuredOutput tool call on your NEXT turn after reading.
    Do not think aloud.

For each result, map:
  id            ← result.id
  title         ← result.title
  url           ← __CONF_URL_RULE__
  space_key     ← the space_key we asked about
  last_modified ← result.lastModified (ISO-ish string)
  author        ← result.author.displayName (best-effort)
  summary       ← result.summary (strip HTML)
  body_excerpt  ← leave empty (we don't need bodies)
  labels        ← [] (we don't query labels)
"""


def _conf_index_system_prompt(settings: BifrostSettings) -> str:
    """Confluence reuses ``sources.jira.base_url``: both live on one Atlassian site."""
    base = settings.sources.jira.base_url.rstrip("/")
    rule = f"result.webUrl OR {base}<_links.webui>" if base else 'result.webUrl (or "")'
    return settings.mcp.render(_CONF_INDEX_SYSTEM_PROMPT.replace("__CONF_URL_RULE__", rule))


_CONF_INDEX_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pages"],
    "properties": {
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["page_id", "title", "url", "space_key"],
                "properties": {
                    "page_id": {"type": "string"},
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "space_key": {"type": "string"},
                    "summary": {"type": "string"},
                    "body_excerpt": {"type": "string"},
                    "last_modified": {"type": "string"},
                    "author": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}


_ATLASSIAN_INDEX_TOOL_NAMES = (
    "search",
    "searchConfluenceUsingCql",
    "getConfluencePage",
    "getPagesInConfluenceSpace",
    "getConfluenceSpaces",
    "searchJiraIssuesUsingJql",
    "getJiraIssue",
    "getVisibleJiraProjects",
    "atlassianUserInfo",
    "getAccessibleAtlassianResources",
)


async def index_confluence_for_repo(
    *,
    session: AsyncSession,
    repo: Repo,
    space_key: str,
    settings: BifrostSettings | None = None,
    max_pages: int = 20,
) -> IndexResult:
    """Enumerate a Confluence space + upsert pages into DB."""
    settings = settings or get_settings()

    user_message = (
        f"---SYSTEM-BOUNDARY---\n"
        f"Repo: {repo.name}\n"
        f"Space to index: {space_key}\n\n"
        f"Return up to {max_pages} of the most useful pages. Use CQL, "
        f"prefer recently-modified + authoritative docs. Skip WMRs and "
        f"meeting notes unless clearly relevant."
    )

    client = ClaudeLlmClient(settings)
    try:
        completion = await client.call_with_json_schema(
            system_prompt=_conf_index_system_prompt(settings),
            user_message=user_message,
            model=settings.agent_model,
            json_schema=_CONF_INDEX_SCHEMA,
            max_turns=6,  # 2 searches + 1 emit + buffer
            timeout_override=180,
            allowed_tools=[*settings.mcp.tools("atlassian", _ATLASSIAN_INDEX_TOOL_NAMES), "Read"],
            inherit_user_mcp=True,
        )
    except ClaudeCliError as exc:
        return IndexResult(
            repo_name=repo.name,
            source="confluence",
            items_indexed=0,
            error=str(exc)[:200],
        )

    pages = (completion.parsed or {}).get("pages") or []
    if not isinstance(pages, list):
        pages = []

    # Upsert rows
    n = 0
    for p in pages:
        if not isinstance(p, dict):
            continue
        try:
            last_mod = _parse_iso(str(p.get("last_modified", "") or ""))
            stmt = (
                pg_insert(ConfluencePage)
                .values(
                    id=_uuid.uuid4(),
                    repo_id=repo.id,
                    space_key=str(p.get("space_key") or space_key),
                    page_id=str(p.get("page_id", "")),
                    title=str(p.get("title", "")),
                    url=str(p.get("url", "")),
                    summary=str(p.get("summary", "") or "")[:2000],
                    body_excerpt=str(p.get("body_excerpt", "") or "")[:4000],
                    last_modified=last_mod,
                    author=str(p.get("author", "") or "")[:200] or None,
                    labels=list(p.get("labels") or []),
                    indexed_at=datetime.now(),
                )
                .on_conflict_do_update(
                    index_elements=["repo_id", "page_id"],
                    set_={
                        "title": str(p.get("title", "")),
                        "url": str(p.get("url", "")),
                        "summary": str(p.get("summary", "") or "")[:2000],
                        "body_excerpt": str(p.get("body_excerpt", "") or "")[:4000],
                        "last_modified": last_mod,
                        "author": str(p.get("author", "") or "")[:200] or None,
                        "labels": list(p.get("labels") or []),
                        "indexed_at": datetime.now(),
                    },
                )
            )
            await session.execute(stmt)
            n += 1
        except Exception:
            logger.exception("confluence upsert failed for %s", p.get("page_id"))

    await _record_sync(session, repo.id, "confluence", n, None)
    await session.commit()
    return IndexResult(repo_name=repo.name, source="confluence", items_indexed=n)


# ── Jira indexer ───────────────────────────────────────────────────


_JIRA_INDEX_SYSTEM_PROMPT = """\
You are a BULK Jira indexer. Minimize tool calls. Your entire job:

  Make exactly TWO searches with SMALL pages (MCP auto-redirects any
  response >~10KB to disk, and the Read tool rejects files >25K tokens,
  so we MUST keep responses inline):

  Call 1:
     jql: project = "<KEY>" AND updated > -90d ORDER BY updated DESC
     maxResults: 10
     fields: summary,status,issuetype,priority,labels,created,updated
     (DO NOT request description or components — they bloat the payload.)

  Call 2 (NEXT PAGE):
     Same JQL, maxResults: 10, startAt: 10

  Then emit the structured output combining both pages. Don't Read any
  temp files — those are a sign the payload was too big. Retry with a
  smaller `maxResults` if that happens.

Hard rules:
  - AT MOST 3 total tool calls (2 searches + 1 emit).
  - No getJiraIssue calls.
  - If response 1 is a file path (too big), RETRY with maxResults=5.

Hard rules — follow exactly:
  - Exactly ONE {mcp:atlassian:searchJiraIssuesUsingJql} call. No more.
  - Do NOT call {mcp:atlassian:getJiraIssue}. Search results already
    carry every field the cache stores.
  - Do NOT refine with extra JQL queries. If the first returned zero,
    emit `issues: []` and stop.
  - You may Read ONE file if the MCP result redirected to disk.
  - Emit the StructuredOutput tool call on your NEXT turn after reading.
    Do not think aloud.

For each hit, map:
  issue_key           ← result.key
  title               ← result.fields.summary
  url                 ← __JIRA_URL_RULE__
  project_key         ← the project key we asked about
  status              ← result.fields.status.name
  issue_type          ← result.fields.issuetype.name
  priority            ← result.fields.priority.name (may be null)
  summary             ← first 200 chars of fields.summary
  description_excerpt ← "" (we skip descriptions)
  labels              ← result.fields.labels (array)
  components          ← [c.name for c in result.fields.components]
  created_at          ← result.fields.created
  updated_at          ← result.fields.updated
"""


def _resolve_issue_url(url: str, key: str, jira: JiraSourceConfig) -> str:
    return url or jira.browse_url(key) or ""


def _jira_index_system_prompt(settings: BifrostSettings) -> str:
    base = settings.sources.jira.base_url.rstrip("/")
    rule = f"{base}/browse/<key>" if base else '""'
    return settings.mcp.render(_JIRA_INDEX_SYSTEM_PROMPT.replace("__JIRA_URL_RULE__", rule))


_JIRA_INDEX_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["issues"],
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["issue_key", "title", "url", "project_key"],
                "properties": {
                    "issue_key": {"type": "string"},
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "project_key": {"type": "string"},
                    "status": {"type": "string"},
                    "issue_type": {"type": "string"},
                    "priority": {"type": "string"},
                    "summary": {"type": "string"},
                    "description_excerpt": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "components": {"type": "array", "items": {"type": "string"}},
                    "created_at": {"type": "string"},
                    "updated_at": {"type": "string"},
                },
            },
        },
    },
}


async def index_jira_for_repo(
    *,
    session: AsyncSession,
    repo: Repo,
    project_key: str,
    settings: BifrostSettings | None = None,
    max_issues: int = 20,
) -> IndexResult:
    """Enumerate recent issues in a Jira project + upsert to DB."""
    settings = settings or get_settings()

    user_message = (
        f"---SYSTEM-BOUNDARY---\n"
        f"Repo: {repo.name}\n"
        f"Jira project to index: {project_key}\n\n"
        f"Return up to {max_issues} of the most useful recent issues. Use JQL, "
        f"scope to updated within last 180 days, prefer epics/incidents/bugs."
    )

    client = ClaudeLlmClient(settings)
    try:
        completion = await client.call_with_json_schema(
            system_prompt=_jira_index_system_prompt(settings),
            user_message=user_message,
            model=settings.agent_model,
            json_schema=_JIRA_INDEX_SCHEMA,
            max_turns=6,  # 2 searches + 1 emit + buffer
            timeout_override=180,
            allowed_tools=[*settings.mcp.tools("atlassian", _ATLASSIAN_INDEX_TOOL_NAMES), "Read"],
            inherit_user_mcp=True,
        )
    except ClaudeCliError as exc:
        return IndexResult(
            repo_name=repo.name,
            source="jira",
            items_indexed=0,
            error=str(exc)[:200],
        )

    issues = (completion.parsed or {}).get("issues") or []
    if not isinstance(issues, list):
        issues = []

    n = 0
    for it in issues:
        if not isinstance(it, dict):
            continue
        try:
            url = _resolve_issue_url(
                str(it.get("url", "")), str(it.get("issue_key", "")), settings.sources.jira
            )
            created = _parse_iso(str(it.get("created_at") or ""))
            updated = _parse_iso(str(it.get("updated_at") or ""))
            stmt = (
                pg_insert(JiraIssue)
                .values(
                    id=_uuid.uuid4(),
                    repo_id=repo.id,
                    project_key=str(it.get("project_key") or project_key),
                    issue_key=str(it.get("issue_key", "")),
                    title=str(it.get("title", "")),
                    url=url,
                    status=str(it.get("status") or "") or None,
                    issue_type=str(it.get("issue_type") or "") or None,
                    priority=str(it.get("priority") or "") or None,
                    summary=str(it.get("summary") or "")[:2000],
                    description_excerpt=str(it.get("description_excerpt") or "")[:4000],
                    labels=list(it.get("labels") or []),
                    components=list(it.get("components") or []),
                    created_at=created,
                    updated_at=updated,
                    indexed_at=datetime.now(),
                )
                .on_conflict_do_update(
                    index_elements=["repo_id", "issue_key"],
                    set_={
                        "title": str(it.get("title", "")),
                        "url": url,
                        "status": str(it.get("status") or "") or None,
                        "issue_type": str(it.get("issue_type") or "") or None,
                        "priority": str(it.get("priority") or "") or None,
                        "summary": str(it.get("summary") or "")[:2000],
                        "description_excerpt": str(it.get("description_excerpt") or "")[:4000],
                        "labels": list(it.get("labels") or []),
                        "components": list(it.get("components") or []),
                        "updated_at": updated,
                        "indexed_at": datetime.now(),
                    },
                )
            )
            await session.execute(stmt)
            n += 1
        except Exception:
            logger.exception("jira upsert failed for %s", it.get("issue_key"))

    await _record_sync(session, repo.id, "jira", n, None)
    await session.commit()
    return IndexResult(repo_name=repo.name, source="jira", items_indexed=n)


# ── Orchestration ──────────────────────────────────────────────────


async def index_repo_external_sources(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    repo_id: str,
    settings: BifrostSettings | None = None,
) -> list[IndexResult]:
    """Index Confluence + Jira for one repo. Uses ownership metadata to
    pick the space/project keys."""
    settings = settings or get_settings()
    rid = _uuid.UUID(repo_id)

    async with session_factory() as session:
        repo = (await session.execute(select(Repo).where(Repo.id == rid))).scalar_one_or_none()
        if repo is None:
            return [
                IndexResult(repo_name="?", source="both", items_indexed=0, error="repo not found")
            ]
        own = (
            await session.execute(select(RepoOwnership).where(RepoOwnership.repo_id == rid))
        ).scalar_one_or_none()

    results: list[IndexResult] = []

    if own and own.confluence_space_key:
        async with session_factory() as s:
            results.append(
                await index_confluence_for_repo(
                    session=s,
                    repo=repo,
                    space_key=own.confluence_space_key,
                    settings=settings,
                )
            )

    if own and own.jira_project_key:
        async with session_factory() as s:
            results.append(
                await index_jira_for_repo(
                    session=s,
                    repo=repo,
                    project_key=own.jira_project_key,
                    settings=settings,
                )
            )

    return results


# ── Helpers ────────────────────────────────────────────────────────


def _parse_iso(s: str) -> datetime | None:
    """Parse an ISO-ish datetime. Returns None on failure."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # Some APIs return "2026-03-15T10:22:33.123Z"
        try:
            return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None


async def _record_sync(
    session: AsyncSession,
    repo_id: _uuid.UUID,
    source: str,
    items_indexed: int,
    error: str | None,
) -> None:
    stmt = (
        pg_insert(ExternalSyncLog)
        .values(
            id=_uuid.uuid4(),
            repo_id=repo_id,
            source=source,
            last_synced_at=datetime.now(),
            items_indexed=items_indexed,
            error=error,
        )
        .on_conflict_do_update(
            index_elements=["repo_id", "source"],
            set_={
                "last_synced_at": datetime.now(),
                "items_indexed": items_indexed,
                "error": error,
            },
        )
    )
    await session.execute(stmt)
