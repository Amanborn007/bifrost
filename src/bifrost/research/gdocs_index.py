"""Offline crawler — pre-populate ``gdoc_pages`` for every repo.

The per-turn ``gdocs_agent`` is expensive: each chat exchange would
otherwise have to do a live Drive MCP search per repo. This module is
the offline counterpart to ``external_indexer.py`` (Confluence + Jira),
running on a cron / one-shot CLI to populate the ``gdoc_pages`` cache
so the agent's ``cached_docs`` parameter has hits and the per-turn
agent only has to fall back to live search when the cache misses.

Strategy (mirrors ``external_indexer.index_confluence_for_repo``):

  1. Load every ``Repo`` row.
  2. Skip repos that have no extracted entities (manifest-only or
     unprocessed repos — indexing them yields noise).
  3. For each remaining repo, spawn one ``claude -p`` subprocess with
     Drive MCP tools and ask it to enumerate up to ~15 candidate design
     docs whose title or body mentions the repo name AND/OR matches the
     design-doc keyword vocabulary (``design``, ``RFC``,
     ``architecture``, ``tech spec``, ``post-mortem``, ``runbook``,
     ``decision``, ``onboarding``).
  4. Filter to ``mimeType=application/vnd.google-apps.document`` and a
     recency cutoff (default: last 2 years).
  5. Upsert each doc into ``gdoc_pages`` (idempotent on
     ``(repo_id, file_id)``).
  6. **Folder inference** — if ≥3 returned docs share the same parent
     folder id, set ``repo_ownership.gdoc_folder_url`` to that folder
     so the next per-turn agent run is folder-scoped (much cheaper).

Why a Claude subprocess and not a direct Drive HTTP call: Drive access
goes through the operator's Drive MCP OAuth; no service account is
needed. Same shape as ``external_indexer`` for the Atlassian MCP.

Run via:

    uv run python -m bifrost.research.gdocs_index --max-repos 5
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid as _uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.cli.config import BifrostSettings, get_settings, validate_startup
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient
from bifrost.storage.models import (
    Entity,
    ExternalSyncLog,
    GDocPage,
    Repo,
    RepoOwnership,
)

logger = logging.getLogger(__name__)


# ── Public types ─────────────────────────────────────────────────────


@dataclass
class IndexStats:
    """Aggregated outcome of a full ``index_gdocs_for_org`` run."""

    repos_processed: int = 0
    docs_indexed: int = 0
    folders_inferred: int = 0
    skipped_empty: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repos_processed": self.repos_processed,
            "docs_indexed": self.docs_indexed,
            "folders_inferred": self.folders_inferred,
            "skipped_empty": self.skipped_empty,
            "errors": list(self.errors),
        }


# ── Tunables ─────────────────────────────────────────────────────────


# Minimum number of docs sharing one parent folder before we promote
# that folder to ``repo_ownership.gdoc_folder_url``. Three felt right:
# a single co-incident match is noise, two is a coincidence, three is
# a pattern. Adjustable via ``index_gdocs_for_org`` kwarg.
DEFAULT_FOLDER_INFERENCE_THRESHOLD = 3

# Default recency cutoff in days. Two years matches the per-turn
# ``gdocs_agent`` prompt's ``modifiedTime > '<2-years-ago>'`` filter.
DEFAULT_RECENCY_CUTOFF_DAYS = 730

# Cap docs per repo so a hot org-wide keyword (e.g. "auth") doesn't
# blow up the cache for one repo. Per-turn agent will still hit the
# top results, and the body-excerpt matcher in the agent does the
# fine-grained ranking.
DEFAULT_MAX_DOCS_PER_REPO = 15


# ── Drive MCP allow-list ─────────────────────────────────────────────


_GDOCS_INDEX_TOOL_NAMES = (
    "search_files",
    "read_file_content",
    "get_file_metadata",
    "list_recent_files",
    "download_file_content",
    "get_file_permissions",
)


# ── System prompt + JSON schema ──────────────────────────────────────


_GDOCS_INDEX_SYSTEM_PROMPT = """\
You are a BULK Google Drive design-doc indexer for ONE repository at a
time. Your output populates an offline cache so a per-turn chat agent
doesn't have to live-search Drive on every question.

You have Drive MCP tools:
  - {mcp:gdrive:search_files} (PRIMARY)
  - {mcp:gdrive:read_file_content} (use SPARINGLY — at
    most for the top-3 most-relevant hits, only when the title is
    ambiguous)
  - {mcp:gdrive:get_file_metadata} (cheap, use to grab
    parents[] / owner / modifiedTime when search snippets are thin)

## Search-query syntax

Drive structured query format. Combine with `and` / `or` / `not`,
strings single-quoted. Common terms:

  * `title contains 'X'` — title substring
  * `fullText contains 'X'` — title OR body match
  * `mimeType = 'application/vnd.google-apps.document'` — restrict to
    Docs (skip sheets / slides / forms)
  * `modifiedTime > 'YYYY-MM-DD'` — recency filter
  * `parents in '<folder_id>'` — folder restriction (only after you've
    inferred a folder)

## Strategy (cap at 3 search calls + 3 reads + 1 emit)

  1. **Search call 1** — title-targeted match for the repo name AND a
     design-doc keyword:
       `mimeType = 'application/vnd.google-apps.document' and
        title contains '<repo-name>' and modifiedTime > '<cutoff>'`
  2. **Search call 2** — fullText fallback (title may not include the
     repo name). Limit page size to 10:
       `mimeType = 'application/vnd.google-apps.document' and
        fullText contains '<repo-name>' and
        (fullText contains 'design' or fullText contains 'RFC' or
         fullText contains 'architecture' or fullText contains 'spec'
         or fullText contains 'postmortem' or fullText contains
         'decision') and modifiedTime > '<cutoff>'`
  3. **Search call 3 (optional)** — if the first two returned <5 hits,
     try repo-name keyword variants (kebab → snake, drop common prefix
     like ``svc-`` / ``app-``).
  4. For each candidate, get `get_file_metadata` if `parents` /
     `modifiedTime` / `owners` aren't already in the search snippet
     (search results often include them inline — DON'T duplicate the
     call).
  5. Body excerpts: `read_file_content` ONLY for the top-3 most
     relevant docs (titles like "Design", "Architecture", "RFC",
     "Decision", "Tech Spec", "Postmortem"). Truncate to first ~2000
     chars when emitting.
  6. Total tool budget: ≤3 searches + ≤3 reads + emit. If response
     redirects to disk (file path), Read once with smaller page.

## Hard rules

  * mimeType MUST be `application/vnd.google-apps.document`. Skip
    sheets/slides — they're not what we cache here.
  * Skip docs older than the recency cutoff in the user message.
  * For each emitted doc, populate `parent_folder_id` from the
    `parents` array (first element). Leave empty if unknown.
  * Cap output at the `max_docs` count in the user message.
  * Emit `docs: []` when nothing matches. Don't fabricate.
  * Do NOT chase links inside doc bodies — that's another agent's job.

---SYSTEM-BOUNDARY---

Respond ONLY by calling the StructuredOutput tool with the schema.
"""


_GDOCS_INDEX_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["docs"],
    "properties": {
        "docs": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["file_id", "title", "url"],
                "properties": {
                    "file_id": {"type": "string"},
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "mime_type": {"type": "string"},
                    "owner_email": {"type": "string"},
                    "summary": {"type": "string"},
                    "body_excerpt": {"type": "string"},
                    "last_modified": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "parent_folder_id": {"type": "string"},
                    "parent_folder_url": {"type": "string"},
                },
            },
        },
        "searched_queries": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}


# ── Per-repo indexer ─────────────────────────────────────────────────


@dataclass
class _RepoIndexResult:
    repo_id: _uuid.UUID
    repo_name: str
    docs: list[dict[str, Any]] = field(default_factory=list)
    inferred_folder_url: str | None = None
    error: str | None = None


async def _index_one_repo(
    *,
    repo: Repo,
    settings: BifrostSettings,
    cutoff: datetime,
    max_docs: int,
    folder_threshold: int,
    client: ClaudeLlmClient | None = None,
) -> _RepoIndexResult:
    """Spawn one ``claude -p`` subprocess and return parsed Drive hits.

    Pure function — does no DB work. Caller is responsible for the
    upsert + ownership update so this layer is easy to unit-test.
    """
    cutoff_str = cutoff.date().isoformat()
    user_message = (
        f"---SYSTEM-BOUNDARY---\n"
        f"Repo: {repo.name}\n"
        f"URN: {repo.urn}\n"
        f"Recency cutoff (modifiedTime >): {cutoff_str}\n"
        f"Max docs to return: {max_docs}\n\n"
        f"Search Google Drive for up to {max_docs} design-doc / RFC / "
        f"architecture / postmortem / decision-record / tech-spec / "
        f"runbook docs that mention `{repo.name}` in title or body. "
        f"Restrict to `application/vnd.google-apps.document`. Skip docs "
        f"modified before {cutoff_str}.\n\n"
        f"For each hit, return parent_folder_id (first entry of `parents`) "
        f"so the offline crawler can promote a shared folder to the "
        f"repo's gdoc_folder_url."
    )

    cli = client or ClaudeLlmClient(settings)
    try:
        completion = await cli.call_with_json_schema(
            system_prompt=settings.mcp.render(_GDOCS_INDEX_SYSTEM_PROMPT),
            user_message=user_message,
            model=settings.agent_model,
            json_schema=_GDOCS_INDEX_SCHEMA,
            max_turns=10,  # 3 searches + 3 reads + emit + buffer
            timeout_override=240,
            allowed_tools=[*settings.mcp.tools("gdrive", _GDOCS_INDEX_TOOL_NAMES), "Read"],
            inherit_user_mcp=True,
        )
    except ClaudeCliError as exc:
        return _RepoIndexResult(
            repo_id=repo.id,
            repo_name=repo.name,
            error=str(exc)[:200],
        )

    raw_docs = (completion.parsed or {}).get("docs") or []
    if not isinstance(raw_docs, list):
        raw_docs = []

    # Cap defensively in case the model overshoots.
    raw_docs = [d for d in raw_docs if isinstance(d, dict)][:max_docs]

    # Filter to Docs mimetype + recency cutoff.
    filtered: list[dict[str, Any]] = []
    for d in raw_docs:
        mt = str(d.get("mime_type") or "application/vnd.google-apps.document")
        if mt != "application/vnd.google-apps.document":
            continue
        last_mod = _parse_iso(str(d.get("last_modified") or ""))
        if last_mod is not None and last_mod < cutoff:
            continue
        filtered.append(d)

    inferred = _infer_shared_folder(filtered, threshold=folder_threshold)
    return _RepoIndexResult(
        repo_id=repo.id,
        repo_name=repo.name,
        docs=filtered,
        inferred_folder_url=inferred,
    )


def _infer_shared_folder(
    docs: list[dict[str, Any]],
    *,
    threshold: int,
) -> str | None:
    """Return a folder URL if ≥``threshold`` docs share one parent.

    Picks the most-common ``parent_folder_id`` across hits. If a doc
    carries an explicit ``parent_folder_url`` we use it; otherwise we
    construct the canonical Drive form ``https://drive.google.com/
    drive/folders/<id>``.
    """
    if not docs or threshold <= 0:
        return None

    counter: Counter[str] = Counter()
    url_by_id: dict[str, str] = {}
    for d in docs:
        fid = str(d.get("parent_folder_id") or "").strip()
        if not fid:
            continue
        counter[fid] += 1
        furl = str(d.get("parent_folder_url") or "").strip()
        if furl and fid not in url_by_id:
            url_by_id[fid] = furl

    if not counter:
        return None

    fid, count = counter.most_common(1)[0]
    if count < threshold:
        return None
    return url_by_id.get(
        fid,
        f"https://drive.google.com/drive/folders/{fid}",
    )


# ── DB persistence ───────────────────────────────────────────────────


async def _upsert_docs(
    session: AsyncSession,
    repo: Repo,
    docs: list[dict[str, Any]],
) -> int:
    """Idempotent upsert of all hits for one repo. Returns row count."""
    n = 0
    now = _utcnow_naive()
    for d in docs:
        try:
            file_id = str(d.get("file_id") or "").strip()
            if not file_id:
                continue
            url = str(d.get("url") or "").strip() or _docs_url_from_id(file_id)
            mime_type = str(d.get("mime_type") or "") or "application/vnd.google-apps.document"
            stmt = (
                pg_insert(GDocPage)
                .values(
                    id=_uuid.uuid4(),
                    repo_id=repo.id,
                    file_id=file_id,
                    title=str(d.get("title") or "")[:500],
                    url=url,
                    mime_type=mime_type,
                    owner_email=(str(d.get("owner_email") or "")[:200] or None),
                    summary=str(d.get("summary") or "")[:2000] or None,
                    body_excerpt=(str(d.get("body_excerpt") or "")[:4000] or None),
                    last_modified=_parse_iso(str(d.get("last_modified") or "")),
                    labels=list(d.get("labels") or []),
                    indexed_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["repo_id", "file_id"],
                    set_={
                        "title": str(d.get("title") or "")[:500],
                        "url": url,
                        "mime_type": mime_type,
                        "owner_email": (str(d.get("owner_email") or "")[:200] or None),
                        "summary": str(d.get("summary") or "")[:2000] or None,
                        "body_excerpt": (str(d.get("body_excerpt") or "")[:4000] or None),
                        "last_modified": _parse_iso(str(d.get("last_modified") or "")),
                        "labels": list(d.get("labels") or []),
                        "indexed_at": now,
                    },
                )
            )
            await session.execute(stmt)
            n += 1
        except Exception:
            logger.exception(
                "gdoc upsert failed for repo=%s file=%s",
                repo.name,
                d.get("file_id"),
            )
    return n


async def _set_gdoc_folder(
    session: AsyncSession,
    repo_id: _uuid.UUID,
    folder_url: str,
) -> bool:
    """Promote an inferred folder URL onto ``repo_ownership``.

    Returns True if a row was updated. Does NOT clobber an existing
    folder URL (manifest-sourced ownership wins; the crawler is the
    fallback)."""
    own = (
        await session.execute(select(RepoOwnership).where(RepoOwnership.repo_id == repo_id))
    ).scalar_one_or_none()

    if own is None:
        # Create a minimal ownership row — same shape ownership_sync.py
        # uses for stub records.
        stmt = (
            pg_insert(RepoOwnership)
            .values(
                repo_id=repo_id,
                gdoc_folder_url=folder_url,
                source="gdocs_index",
            )
            .on_conflict_do_update(
                index_elements=["repo_id"],
                set_={"gdoc_folder_url": folder_url},
            )
        )
        await session.execute(stmt)
        return True

    if own.gdoc_folder_url:
        return False
    own.gdoc_folder_url = folder_url
    await session.flush()
    return True


# ── Skip-empty-repo gate ─────────────────────────────────────────────


async def _repo_has_entities(
    session: AsyncSession,
    repo_id: _uuid.UUID,
) -> bool:
    """True iff at least one Entity is scoped to this repo.

    The crawler skips repos with no entities — they're either
    manifest-only registrations or extraction hasn't run, and either
    way the resulting Drive cache would be unused (the per-turn agent
    needs entities to construct prompts)."""
    n = (
        await session.execute(select(func.count(Entity.id)).where(Entity.scope_repo_id == repo_id))
    ).scalar_one()
    return int(n or 0) > 0


# ── Top-level orchestration ──────────────────────────────────────────


async def index_gdocs_for_org(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: BifrostSettings | None = None,
    max_repos: int | None = None,
    recency_cutoff_days: int = DEFAULT_RECENCY_CUTOFF_DAYS,
    max_docs_per_repo: int = DEFAULT_MAX_DOCS_PER_REPO,
    folder_inference_threshold: int = DEFAULT_FOLDER_INFERENCE_THRESHOLD,
    indexer: Any | None = None,
) -> IndexStats:
    """Crawl Drive for design docs and pre-populate ``gdoc_pages``.

    Args:
        session_factory: async DB session factory.
        settings: BifrostSettings (loads from env if omitted).
        max_repos: cap repos processed for testing — ``None`` means
            "every repo".
        recency_cutoff_days: only index docs modified within this
            many days. 730 (~2y) by default.
        max_docs_per_repo: per-repo cap on hits we ask the LLM for.
        folder_inference_threshold: how many docs must share a parent
            folder before we promote that folder onto the
            repo_ownership row.
        indexer: optional override for the per-repo indexer (used by
            tests to inject a stub instead of spawning ``claude -p``).
            Signature: ``async def indexer(*, repo, settings, cutoff,
            max_docs, folder_threshold) -> _RepoIndexResult``.

    Returns:
        IndexStats with per-call counters + any error strings.
    """
    settings = settings or get_settings()
    runner = indexer or _index_one_repo
    cutoff = _utcnow_naive() - timedelta(days=recency_cutoff_days)

    stats = IndexStats()

    # 1. Pull repos to consider — eligibility checked per-repo to keep
    # the gate cheap when ``max_repos`` is small.
    async with session_factory() as session:
        repos_q = select(Repo).order_by(Repo.name)
        repos = list((await session.execute(repos_q)).scalars().all())

    if max_repos is not None:
        repos = repos[:max_repos]

    for repo in repos:
        async with session_factory() as session:
            has_entities = await _repo_has_entities(session, repo.id)
        if not has_entities:
            stats.skipped_empty += 1
            logger.info(
                "gdocs_index: skipping %s — no entities yet",
                repo.name,
            )
            continue

        try:
            result = await runner(
                repo=repo,
                settings=settings,
                cutoff=cutoff,
                max_docs=max_docs_per_repo,
                folder_threshold=folder_inference_threshold,
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("gdocs_index: %s failed", repo.name)
            stats.errors.append(f"{repo.name}: {exc}"[:200])
            continue

        stats.repos_processed += 1
        if result.error:
            stats.errors.append(f"{repo.name}: {result.error}"[:200])
            # Even on partial error we still record an ExternalSyncLog
            # with the error so /indexer/status surfaces it.
            async with session_factory() as session:
                await _record_sync(
                    session,
                    repo.id,
                    "gdocs",
                    0,
                    result.error,
                )
                await session.commit()
            continue

        async with session_factory() as session:
            n = await _upsert_docs(session, repo, result.docs)
            stats.docs_indexed += n

            if result.inferred_folder_url:
                updated = await _set_gdoc_folder(
                    session,
                    repo.id,
                    result.inferred_folder_url,
                )
                if updated:
                    stats.folders_inferred += 1

            await _record_sync(session, repo.id, "gdocs", n, None)
            await session.commit()

        logger.info(
            "gdocs_index: %s — %d docs indexed%s",
            repo.name,
            n,
            (
                f" (folder inferred: {result.inferred_folder_url})"
                if result.inferred_folder_url
                else ""
            ),
        )

    return stats


# ── Helpers ──────────────────────────────────────────────────────────


def _docs_url_from_id(file_id: str) -> str:
    """Canonical Drive Doc URL for a file id."""
    return f"https://docs.google.com/document/d/{file_id}"


def _utcnow_naive() -> datetime:
    """Naive UTC ``datetime`` — matches the ``TIMESTAMP WITHOUT TIME
    ZONE`` shape declared on the cache tables (``last_synced_at``,
    ``last_modified``, ``indexed_at``). Same convention used by
    ``external_indexer.py``. Avoids the Python 3.12
    ``datetime.utcnow()`` deprecation by going through aware
    ``datetime.now(UTC)`` and stripping tzinfo at the boundary."""
    return datetime.now(UTC).replace(tzinfo=None)


def _parse_iso(s: str) -> datetime | None:
    """Parse an ISO-ish datetime → naive UTC.

    Returns None on failure. Drive returns RFC3339 like
    ``2026-04-12T15:22:33.456Z``; we strip tzinfo and store as naive
    UTC because the ``last_modified`` / ``last_synced_at`` columns are
    declared without ``timezone=True`` (matching the rest of the cache
    tables — ConfluencePage, JiraIssue).
    """
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


async def _record_sync(
    session: AsyncSession,
    repo_id: _uuid.UUID,
    source: str,
    items_indexed: int,
    error: str | None,
) -> None:
    """Mirror ``external_indexer._record_sync`` for the ``gdocs`` source."""
    now = _utcnow_naive()
    stmt = (
        pg_insert(ExternalSyncLog)
        .values(
            id=_uuid.uuid4(),
            repo_id=repo_id,
            source=source,
            last_synced_at=now,
            items_indexed=items_indexed,
            error=error,
        )
        .on_conflict_do_update(
            index_elements=["repo_id", "source"],
            set_={
                "last_synced_at": now,
                "items_indexed": items_indexed,
                "error": error,
            },
        )
    )
    await session.execute(stmt)


# ── CLI entry point ──────────────────────────────────────────────────


async def _async_main(args: argparse.Namespace) -> int:
    settings = validate_startup()
    # Local import to avoid pulling FastAPI deps when used as a library.
    from bifrost.api.deps import get_session_factory

    sf = get_session_factory(settings)
    stats = await index_gdocs_for_org(
        session_factory=sf,
        settings=settings,
        max_repos=args.max_repos,
        recency_cutoff_days=args.recency_days,
        max_docs_per_repo=args.max_docs,
        folder_inference_threshold=args.folder_threshold,
    )
    print(  # noqa: T201 — CLI output, intentional
        "gdocs_index complete: "
        f"repos={stats.repos_processed} "
        f"docs={stats.docs_indexed} "
        f"folders={stats.folders_inferred} "
        f"skipped_empty={stats.skipped_empty} "
        f"errors={len(stats.errors)}"
    )
    if stats.errors:
        for e in stats.errors[:10]:
            print(f"  ! {e}")  # noqa: T201
    return 0 if not stats.errors else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bifrost-gdocs-index",
        description=(
            "Offline Google Drive design-doc crawler. Pre-populates "
            "gdoc_pages so the per-turn gdocs_agent doesn't have to "
            "live-search Drive on every chat exchange."
        ),
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=None,
        help="Cap repos processed (default: every repo)",
    )
    parser.add_argument(
        "--recency-days",
        type=int,
        default=DEFAULT_RECENCY_CUTOFF_DAYS,
        help="Only index docs modified within N days "
        f"(default: {DEFAULT_RECENCY_CUTOFF_DAYS} ≈ 2 years)",
    )
    parser.add_argument(
        "--max-docs",
        type=int,
        default=DEFAULT_MAX_DOCS_PER_REPO,
        help=f"Per-repo doc cap (default: {DEFAULT_MAX_DOCS_PER_REPO})",
    )
    parser.add_argument(
        "--folder-threshold",
        type=int,
        default=DEFAULT_FOLDER_INFERENCE_THRESHOLD,
        help="Min docs sharing a parent folder before promoting the "
        "folder onto repo_ownership.gdoc_folder_url (default: "
        f"{DEFAULT_FOLDER_INFERENCE_THRESHOLD})",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="DEBUG | INFO | WARNING",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    return asyncio.run(_async_main(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
