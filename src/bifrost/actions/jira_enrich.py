"""Auto-enrich a task from its Jira key.

When a user creates a task by pasting a Jira URL or including a
``jira_key``, we can pre-populate the task's ``acceptance_criteria``
and surface the ticket's description + linked Confluence pages — no
more "go look at the ticket, come back, tell me what it says."

Sources of truth (in order)
---------------------------
1. **Local ``jira_issues`` cache** — built by the external indexer.
   Fast, free, may be stale by a day or two.
2. **Live ``JiraAdapter`` fetch** — authoritative, requires
   ``sources.jira.base_url`` + ``sources.jira.email`` +
   ``sources.jira.api_token`` settings. Skipped if credentials aren't configured.
3. **Fallback** — leave the task unenriched with a note.

Enrichment fields persisted on ``tasks``
-----------------------------------------
* ``jira_enrichment`` (JSONB) — the full fetched payload so the UI
  can render a rich summary (title, status, priority, labels,
  components, description excerpt, linked Confluence URLs).
* ``acceptance_criteria`` (existing JSONB array) — we parse the
  description for "Acceptance Criteria" sections + checklist
  bullets, and populate the column if it's empty. Never overwrite
  an AC list the user has already curated.
* ``enriched_at`` — timestamp for UI freshness hints.

What we DON'T do
----------------
* No outbound write to Jira. This module reads only.
* No changes to ``intent_text``. Users typed that; we keep it.
* No credential prompts. Missing config → silent skip + note.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.actions.task_lifecycle import record_event
from bifrost.cli.config import get_settings
from bifrost.cli.config_sections import ConfluenceSourceConfig, JiraSourceConfig
from bifrost.errors import ConfigError

logger = logging.getLogger(__name__)


# ── Parsing ────────────────────────────────────────────────────────


_AC_SECTION_RE = re.compile(
    r"""
    ^\s*                              # leading whitespace
    (?:\#{1,6}\s+)?                   # optional markdown heading
    (?:\*\*|__)?                      # optional bold
    (?:Acceptance\s+Criteria|AC|Definition\s+of\s+Done|DoD)
    (?:\*\*|__)?                      # optional bold close
    \s*:?\s*$                         # optional colon
    """,
    re.MULTILINE | re.VERBOSE | re.IGNORECASE,
)

_BULLET_RE = re.compile(
    r"^\s*(?:[-*•]|\d+\.)\s+(?:\[[ xX]\]\s+)?(?P<body>.+?)\s*$",
    re.MULTILINE,
)

_CONFLUENCE_CLOUD_ROOT = r"https?://[\w.-]+\.atlassian\.net/wiki"
_CONFLUENCE_PATH = r"/[\w/%.?=&#:+-]+"


@lru_cache(maxsize=32)
def _confluence_url_re(root_pattern: str) -> re.Pattern[str]:
    return re.compile(root_pattern + _CONFLUENCE_PATH)


_JIRA_CLOUD_HOST = r"[\w.-]+\.atlassian\.net"


@lru_cache(maxsize=32)
def _jira_url_re(host_pattern: str) -> re.Pattern[str]:
    return re.compile(rf"https?://{host_pattern}/browse/([A-Z][A-Z0-9_]+-\d+)")


def extract_acceptance_criteria(description: str, *, max_items: int = 20) -> list[str]:
    """Pull bullet-pointed ACs out of a Jira description.

    Accepts any of:
        * markdown ``## Acceptance Criteria`` block followed by bullets
        * ``AC:`` / ``Acceptance Criteria:`` line followed by bullets
        * A standalone list of checkbox-style bullets like ``- [ ] foo``
    """
    if not description:
        return []

    items: list[str] = []

    # Find every AC section header and scan bullets until the next header.
    for m in _AC_SECTION_RE.finditer(description):
        tail = description[m.end() :]
        # Stop at the next blank-then-non-bullet header or heading.
        section = _take_bullets(tail)
        for b in _BULLET_RE.finditer(section):
            body = b.group("body").strip()
            if body:
                items.append(body)
            if len(items) >= max_items:
                return items

    # Also pick up any checkbox bullets in the whole description
    # (useful when the AC section header is absent).
    if not items:
        for b in _BULLET_RE.finditer(description):
            body = b.group("body").strip()
            # Only count it if there's a checkbox marker to avoid
            # sweeping in every bullet in the description.
            if re.match(
                r"^\[[ xX]\]", b.group(0).strip().lstrip("-*• \t").lstrip("0123456789. \t")
            ):
                if body:
                    items.append(body)
                if len(items) >= max_items:
                    break

    # Dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _take_bullets(text_after_header: str) -> str:
    """Return the slice of ``text_after_header`` up to the next heading.

    Stops on a markdown heading (``#``), a new underlined heading, or
    two consecutive blank lines followed by non-bullet content.
    """
    lines = text_after_header.splitlines()
    end = len(lines)
    blank_streak = 0
    in_list = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            end = i
            break
        if _BULLET_RE.match(line):
            in_list = True
            blank_streak = 0
            continue
        if not stripped:
            blank_streak += 1
            if in_list and blank_streak >= 2:
                end = i
                break
            continue
        if in_list:
            # Non-bullet content after we'd entered the list — stop.
            end = i
            break
    return "\n".join(lines[:end])


def extract_confluence_links(
    description: str, *, confluence: ConfluenceSourceConfig | None = None
) -> list[str]:
    """Return unique Confluence URLs mentioned in the text."""
    if not description:
        return []
    confluence = confluence or get_settings().sources.confluence
    seen: set[str] = set()
    out: list[str] = []
    for m in _confluence_url_re(confluence.url_pattern(_CONFLUENCE_CLOUD_ROOT)).finditer(
        description
    ):
        url = m.group(0)
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def extract_related_jira_keys(
    description: str, *, skip: str | None = None, jira: JiraSourceConfig | None = None
) -> list[str]:
    """Return Jira ticket keys mentioned in the text (excluding ``skip``)."""
    if not description:
        return []
    jira = jira or get_settings().sources.jira
    seen: set[str] = set()
    if skip:
        seen.add(skip)
    out: list[str] = []
    for m in _jira_url_re(jira.host_pattern(_JIRA_CLOUD_HOST)).finditer(description):
        key = m.group(1).upper()
        if key not in seen:
            seen.add(key)
            out.append(key)
    # Also catch bare keys like "INV-1234"
    for m in re.finditer(r"\b([A-Z]{2,10}-\d+)\b", description):
        key = m.group(1).upper()
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


# ── Result types ────────────────────────────────────────────────────


@dataclass
class JiraEnrichment:
    jira_key: str
    title: str | None = None
    status: str | None = None
    priority: str | None = None
    issue_type: str | None = None
    url: str | None = None
    description_excerpt: str | None = None
    labels: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    confluence_links: list[str] = field(default_factory=list)
    related_keys: list[str] = field(default_factory=list)
    source: str = "cache"  # cache | live | none
    fetched_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EnrichmentResult:
    task_id: str
    jira_key: str | None
    enriched: bool
    enrichment: dict[str, Any] | None
    acceptance_criteria_added: int
    note: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Entry point ─────────────────────────────────────────────────────


async def enrich_task_from_jira(
    session: AsyncSession,
    *,
    task_id: str,
    force_refresh: bool = False,
) -> EnrichmentResult:
    """Pull Jira data for a task, parse ACs + Confluence links, persist.

    Returns a structured result either way. When the task has no
    ``jira_key`` we short-circuit with ``enriched=False`` and a note
    instead of raising — callers can fire-and-forget without handling
    "this task wasn't linked to Jira."
    """
    task = (
        await session.execute(
            text(
                """
                SELECT id, jira_key, acceptance_criteria, enriched_at,
                       intent_text
                FROM tasks WHERE id = :id
                """
            ),
            {"id": task_id},
        )
    ).first()
    if task is None:
        raise ValueError(f"task {task_id} not found")

    jira_key = task[1]
    if not jira_key:
        return EnrichmentResult(
            task_id=task_id,
            jira_key=None,
            enriched=False,
            enrichment=None,
            acceptance_criteria_added=0,
            note="Task has no jira_key — nothing to enrich.",
        )

    if task[3] is not None and not force_refresh:
        # Already enriched recently; no-op unless forced.
        enrichment_row = (
            await session.execute(
                text("SELECT jira_enrichment FROM tasks WHERE id = :id"),
                {"id": task_id},
            )
        ).first()
        return EnrichmentResult(
            task_id=task_id,
            jira_key=jira_key,
            enriched=True,
            enrichment=(enrichment_row[0] if enrichment_row else None),
            acceptance_criteria_added=0,
            note="Already enriched (set force_refresh=true to re-fetch).",
        )

    # 1. Cache lookup
    enrichment = await _load_from_cache(session, jira_key)

    # 2. Live fetch (optional)
    if enrichment is None:
        enrichment = await _live_fetch(jira_key)

    if enrichment is None:
        return EnrichmentResult(
            task_id=task_id,
            jira_key=jira_key,
            enriched=False,
            enrichment=None,
            acceptance_criteria_added=0,
            note=(
                "No data for this issue — not in local cache and Jira "
                "live-fetch is not configured (set sources.jira.base_url, "
                "sources.jira.email and sources.jira.api_token to enable)."
            ),
        )

    existing_ac = list(task[2] or [])
    add_ac = 0
    if not existing_ac and enrichment.acceptance_criteria:
        existing_ac = list(enrichment.acceptance_criteria)
        add_ac = len(existing_ac)

    now = datetime.now(UTC)
    await session.execute(
        text(
            """
            UPDATE tasks
            SET jira_enrichment = CAST(:enr AS JSONB),
                acceptance_criteria = CAST(:ac AS JSONB),
                enriched_at = :at,
                updated_at = NOW()
            WHERE id = :id
            """
        ),
        {
            "enr": json.dumps(enrichment.to_dict()),
            "ac": json.dumps(existing_ac),
            "at": now,
            "id": task_id,
        },
    )
    await record_event(
        session,
        task_id=task_id,
        actor="system",
        action="jira_enriched",
        payload={
            "jira_key": jira_key,
            "source": enrichment.source,
            "ac_added": add_ac,
            "confluence_links": len(enrichment.confluence_links),
        },
    )
    return EnrichmentResult(
        task_id=task_id,
        jira_key=jira_key,
        enriched=True,
        enrichment=enrichment.to_dict(),
        acceptance_criteria_added=add_ac,
        note=(
            f"Enriched from {enrichment.source}. "
            + (
                f"Populated {add_ac} AC items. "
                if add_ac
                else "AC list already present; left alone. "
            )
            + (
                f"{len(enrichment.confluence_links)} Confluence link(s) detected."
                if enrichment.confluence_links
                else ""
            )
        ).strip(),
    )


# ── Source fetchers ────────────────────────────────────────────────


async def _load_from_cache(
    session: AsyncSession,
    jira_key: str,
) -> JiraEnrichment | None:
    row = (
        await session.execute(
            text(
                """
                SELECT title, status, priority, issue_type, url,
                       description_excerpt, labels, components,
                       updated_at
                FROM jira_issues
                WHERE issue_key = :k
                ORDER BY updated_at DESC NULLS LAST, indexed_at DESC
                LIMIT 1
                """
            ),
            {"k": jira_key},
        )
    ).first()
    if row is None:
        return None
    desc = row[5] or ""
    return JiraEnrichment(
        jira_key=jira_key,
        title=row[0],
        status=row[1],
        priority=row[2],
        issue_type=row[3],
        url=row[4],
        description_excerpt=desc[:4000],
        labels=list(row[6] or []),
        components=list(row[7] or []),
        acceptance_criteria=extract_acceptance_criteria(desc),
        confluence_links=extract_confluence_links(desc),
        related_keys=extract_related_jira_keys(desc, skip=jira_key),
        source="cache",
        fetched_at=(row[8].isoformat() if row[8] else ""),
    )


async def _live_fetch(jira_key: str) -> JiraEnrichment | None:
    """Fetch a single issue via JiraAdapter if ``sources.jira`` credentials are set."""
    jira = get_settings().sources.jira
    if jira.email is None or jira.api_token is None:
        return None
    if not jira.base_url:
        raise ConfigError(
            "sources.jira.email and api_token are set but sources.jira.base_url is empty"
        )
    base_url = jira.base_url.rstrip("/")
    email = jira.email
    token = jira.api_token.get_secret_value()

    try:
        from bifrost.adapters.jira import JiraAdapter
    except Exception:
        logger.exception("jira adapter import failed")
        return None

    try:
        adapter = JiraAdapter(base_url=base_url, email=email, token=token)
        issue = await adapter._get_issue(jira_key)  # type: ignore[attr-defined]
    except Exception as e:
        logger.warning("live jira fetch failed for %s: %s", jira_key, e)
        return None

    fields = issue.get("fields", {}) or {}
    description = ""
    raw_desc = fields.get("description")
    if isinstance(raw_desc, dict):
        # ADF → strip to plain text (rough)
        description = _flatten_adf(raw_desc)
    elif isinstance(raw_desc, str):
        description = raw_desc
    description = (description or "").strip()

    return JiraEnrichment(
        jira_key=jira_key,
        title=fields.get("summary"),
        status=(fields.get("status") or {}).get("name"),
        priority=(fields.get("priority") or {}).get("name"),
        issue_type=(fields.get("issuetype") or {}).get("name"),
        url=f"{base_url}/browse/{jira_key}",
        description_excerpt=description[:4000],
        labels=list(fields.get("labels") or []),
        components=[c.get("name", "") for c in (fields.get("components") or []) if c.get("name")],
        acceptance_criteria=extract_acceptance_criteria(description),
        confluence_links=extract_confluence_links(description),
        related_keys=extract_related_jira_keys(description, skip=jira_key),
        source="live",
        fetched_at=datetime.now(UTC).isoformat(),
    )


def _flatten_adf(node: Any) -> str:
    """Flatten Atlassian Document Format to plain-ish markdown."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    out: list[str] = []
    if node.get("type") == "text":
        txt = node.get("text") or ""
        marks = {m.get("type") for m in (node.get("marks") or []) if isinstance(m, dict)}
        if "code" in marks:
            txt = f"`{txt}`"
        return txt
    for child in node.get("content") or []:
        out.append(_flatten_adf(child))
    ntype = node.get("type")
    joined = "".join(out)
    if ntype == "heading":
        level = int((node.get("attrs") or {}).get("level", 2))
        return "\n" + ("#" * level) + " " + joined + "\n"
    if ntype == "paragraph":
        return joined + "\n\n"
    if ntype == "bulletList":
        return joined
    if ntype == "orderedList":
        return joined
    if ntype == "listItem":
        return "- " + joined.rstrip() + "\n"
    return joined
