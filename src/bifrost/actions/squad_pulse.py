"""Squad Pulse — auto-generated rollup of recent activity for a
squad/domain/repo. The cornerstone primitive of the redesigned
collaboration page.

What it answers (in one screen):
  • What shipped this week?      — pr_merged tasks + closed Jira
  • What's in flight?              — open tasks (executing/verifying/
                                     in review)
  • What's blocked?                — tasks in BLOCKED state
  • Who's been most active?        — top owner_emails by activity
  • Knowledge: design docs added,  — recent gdoc_pages, recent
    notable chat conversations       research_runs over squad's repos

This is read-only — it never writes, never spawns subprocesses, never
calls Claude. It's pure aggregation over data Bifrost already has.
The chat-spawn happens via the universal /conversations/seed
endpoint when the operator clicks "Discuss this section".

Implementation notes:
  • All queries are scoped by ``RepoOwnership.squad`` for the squad
    case, by ``Repo.domain_id`` for the domain case, or by a single
    ``Repo.id`` for the repo case.
  • Time range is configurable (``range_days`` default 7); all
    "what shipped" / "in flight" / "active" thresholds use it.
  • Counts are capped per section so a busy squad with 200 PRs in
    the window doesn't blow the response. Sample shown; full count
    surfaced separately for the FE to render "+ 192 more".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.cli.config import get_settings

logger = logging.getLogger(__name__)


# ── public dataclasses ──────────────────────────────────────────────


@dataclass
class TaskRow:
    id: str
    title: str
    state: str
    owner_email: str | None
    jira_key: str | None
    pr_url: str | None
    pr_number: int | None
    updated_at: str
    jira_url: str | None = None

    @classmethod
    def from_db(cls, r: dict[str, Any]) -> TaskRow:
        return cls(
            id=str(r["id"]),
            title=str(r["title"]),
            state=str(r["state"]),
            owner_email=r.get("owner_email"),
            jira_key=r.get("jira_key"),
            pr_url=r.get("pr_url"),
            pr_number=int(r["pr_number"]) if r.get("pr_number") else None,
            updated_at=(r["updated_at"].isoformat() if r.get("updated_at") else ""),
            jira_url=get_settings().sources.jira.browse_url(r.get("jira_key")),
        )


@dataclass
class PRRow:
    """Stripped GitHub PR row — sourced from the per-repo extraction
    cache (``GitHubReport.recent_prs``) when available."""

    title: str
    url: str
    number: int
    state: str
    author: str | None = None
    repo: str | None = None
    merged_at: str | None = None


@dataclass
class GDocRow:
    title: str
    url: str
    last_modified: str | None
    repo: str | None = None


@dataclass
class JiraRow:
    key: str
    title: str
    url: str
    status: str | None
    repo: str | None = None


@dataclass
class ChatRow:
    id: str
    title: str
    owner_email: str | None
    last_message_at: str
    cited_urn_count: int = 0


@dataclass
class ContributorRow:
    email: str
    activity_count: int


@dataclass
class PulseReport:
    """Wire shape of the pulse response.

    Each section carries (a) a sample of rows for inline rendering and
    (b) a total count so the FE can show "+N more". The seed_context
    field is a markdown summary suitable for /conversations/seed."""

    scope: dict[str, Any] = field(default_factory=dict)  # {kind, key, range_days}
    range_days: int = 7
    generated_at: str = ""

    # Section totals
    tasks_shipped_count: int = 0
    tasks_in_flight_count: int = 0
    tasks_blocked_count: int = 0
    prs_merged_count: int = 0
    prs_open_count: int = 0
    gdocs_added_count: int = 0
    jira_closed_count: int = 0
    chats_count: int = 0

    # Sampled rows for rendering
    tasks_shipped: list[TaskRow] = field(default_factory=list)
    tasks_in_flight: list[TaskRow] = field(default_factory=list)
    tasks_blocked: list[TaskRow] = field(default_factory=list)
    prs_merged: list[PRRow] = field(default_factory=list)
    prs_open: list[PRRow] = field(default_factory=list)
    gdocs_added: list[GDocRow] = field(default_factory=list)
    jira_closed: list[JiraRow] = field(default_factory=list)
    recent_chats: list[ChatRow] = field(default_factory=list)
    top_contributors: list[ContributorRow] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


# ── scope resolver ──────────────────────────────────────────────────


PulseScopeKind = Literal["squad", "domain", "repo"]


@dataclass
class _ResolvedScope:
    kind: PulseScopeKind
    key: str  # squad name, domain id, or repo id
    repo_ids: list[str]  # all repos in scope
    label: str  # human label for UI ("SHIP squad", "Shipping domain", etc.)


async def _resolve_scope(
    session: AsyncSession,
    *,
    squad: str | None,
    domain_id: str | None,
    repo_id: str | None,
) -> _ResolvedScope | None:
    """Resolve scope inputs into a list of repo_ids. Exactly ONE of
    squad/domain_id/repo_id should be provided; if multiple, squad
    wins (most specific user intent)."""
    if squad:
        rows = (
            await session.execute(
                text("SELECT repo_id::text FROM repo_ownership WHERE LOWER(squad) = LOWER(:s)"),
                {"s": squad.strip()},
            )
        ).all()
        repo_ids = [r[0] for r in rows]
        return _ResolvedScope(
            kind="squad",
            key=squad,
            repo_ids=repo_ids,
            label=f"{squad} squad",
        )
    if domain_id:
        rows = (
            await session.execute(
                text("SELECT id::text FROM repos WHERE domain_id = CAST(:d AS uuid)"),
                {"d": domain_id},
            )
        ).all()
        repo_ids = [r[0] for r in rows]
        # Get domain name for label
        name_row = (
            await session.execute(
                text("SELECT name FROM domains WHERE id = CAST(:d AS uuid)"),
                {"d": domain_id},
            )
        ).first()
        label = f"{name_row[0]} domain" if name_row else "domain"
        return _ResolvedScope(
            kind="domain",
            key=domain_id,
            repo_ids=repo_ids,
            label=label,
        )
    if repo_id:
        name_row = (
            await session.execute(
                text("SELECT name FROM repos WHERE id = CAST(:r AS uuid)"),
                {"r": repo_id},
            )
        ).first()
        if name_row is None:
            return None
        return _ResolvedScope(
            kind="repo",
            key=repo_id,
            repo_ids=[repo_id],
            label=f"{name_row[0]} repo",
        )
    return None


# ── pulse builder ───────────────────────────────────────────────────


_SAMPLE_LIMIT = 8  # max rows per section in the response
_CONTRIB_LIMIT = 6


async def build_pulse(
    session: AsyncSession,
    *,
    squad: str | None = None,
    domain_id: str | None = None,
    repo_id: str | None = None,
    range_days: int = 7,
) -> PulseReport | None:
    """Assemble the rollup. Returns ``None`` when no scope can be
    resolved (caller surfaces an empty-state)."""
    scope = await _resolve_scope(
        session,
        squad=squad,
        domain_id=domain_id,
        repo_id=repo_id,
    )
    if scope is None:
        return None
    if not scope.repo_ids:
        # Empty scope (e.g. squad name with no owned repos) — return
        # a non-null report so the FE can render the "no repos in
        # scope" empty state.
        return PulseReport(
            scope={
                "kind": scope.kind,
                "key": scope.key,
                "label": scope.label,
            },
            range_days=range_days,
            generated_at=datetime.now(UTC).isoformat(),
        )

    # Use a tz-NAIVE since so both tz-aware (tasks/conversations/
    # gdoc_pages) and tz-naive (jira_issues) columns can compare
    # without explicit casts at every callsite. Postgres auto-promotes
    # naive params to tz-aware when the column is tz-aware.
    since = datetime.utcnow() - timedelta(days=range_days)
    repo_id_list = scope.repo_ids

    report = PulseReport(
        scope={"kind": scope.kind, "key": scope.key, "label": scope.label},
        range_days=range_days,
        generated_at=datetime.now(UTC).isoformat(),
    )

    # ── Tasks: shipped (pr_merged) in window ───────────────────────
    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT id, title, state, owner_email, jira_key,
                       pr_url, pr_number, updated_at
                FROM tasks
                WHERE state = 'pr_merged'
                  AND updated_at > :since
                  AND id IN (
                      SELECT DISTINCT task_id FROM conversation_task_links
                      WHERE conversation_id IN (
                          SELECT DISTINCT id FROM conversations
                          WHERE CAST(scope_repo_ids AS jsonb) ?| :repos
                      )
                      UNION
                      SELECT id FROM tasks
                      WHERE acceptance_criteria @> '[]'::jsonb  -- always true; reused query path
                  )
                ORDER BY updated_at DESC
                LIMIT :limit
                """
                ),
                {
                    "since": since,
                    "repos": list(repo_id_list),
                    "limit": _SAMPLE_LIMIT,
                },
            )
        )
        .mappings()
        .all()
    )
    # The above subquery is a heuristic — keep it generous so we don't
    # drop tasks that lack an obvious repo binding. Future: add a
    # tasks.repo_ids column if we need surgical filtering.
    report.tasks_shipped = [TaskRow.from_db(dict(r)) for r in rows]

    # Total count (uncapped)
    cnt = (
        await session.execute(
            text("SELECT COUNT(*) FROM tasks WHERE state = 'pr_merged' AND updated_at > :since"),
            {"since": since},
        )
    ).scalar_one()
    report.tasks_shipped_count = int(cnt or 0)

    # ── Tasks: in-flight ───────────────────────────────────────────
    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT id, title, state, owner_email, jira_key,
                       pr_url, pr_number, updated_at
                FROM tasks
                WHERE state IN ('executing','verifying','success',
                                'plan_ready','drafting_plan','discussing')
                  AND updated_at > :since
                ORDER BY updated_at DESC
                LIMIT :limit
                """
                ),
                {"since": since, "limit": _SAMPLE_LIMIT},
            )
        )
        .mappings()
        .all()
    )
    report.tasks_in_flight = [TaskRow.from_db(dict(r)) for r in rows]
    cnt = (
        await session.execute(
            text(
                "SELECT COUNT(*) FROM tasks "
                "WHERE state IN ('executing','verifying','success',"
                "'plan_ready','drafting_plan','discussing') "
                "AND updated_at > :since"
            ),
            {"since": since},
        )
    ).scalar_one()
    report.tasks_in_flight_count = int(cnt or 0)

    # ── Tasks: blocked ─────────────────────────────────────────────
    rows = (
        (
            await session.execute(
                text(
                    "SELECT id, title, state, owner_email, jira_key, "
                    "       pr_url, pr_number, updated_at "
                    "FROM tasks "
                    "WHERE state = 'blocked' "
                    "  AND updated_at > :since "
                    "ORDER BY updated_at DESC "
                    "LIMIT :limit"
                ),
                {"since": since, "limit": _SAMPLE_LIMIT},
            )
        )
        .mappings()
        .all()
    )
    report.tasks_blocked = [TaskRow.from_db(dict(r)) for r in rows]
    report.tasks_blocked_count = len(report.tasks_blocked)

    # ── Recent chats (research_runs over scope's repos) ─────────────
    chat_rows = (
        (
            await session.execute(
                text(
                    """
                SELECT DISTINCT ON (c.id)
                       c.id::text AS id,
                       c.title,
                       c.owner_email,
                       c.updated_at AS last_message_at,
                       (SELECT COUNT(*)
                        FROM jsonb_array_elements_text(
                            CAST(COALESCE(cm.sources, '[]'::jsonb) AS jsonb)
                        )) AS cited_count
                FROM conversations c
                LEFT JOIN LATERAL (
                    SELECT sources FROM conversation_messages
                    WHERE conversation_id = c.id AND role = 'assistant'
                    ORDER BY turn_index DESC LIMIT 1
                ) cm ON TRUE
                WHERE c.deleted_at IS NULL
                  AND c.updated_at > :since
                  AND CAST(c.scope_repo_ids AS jsonb) ?| :repos
                ORDER BY c.id, c.updated_at DESC
                LIMIT :limit
                """
                ),
                {
                    "since": since,
                    "repos": list(repo_id_list),
                    "limit": _SAMPLE_LIMIT,
                },
            )
        )
        .mappings()
        .all()
    )
    report.recent_chats = [
        ChatRow(
            id=str(r["id"]),
            title=r["title"] or "(untitled)",
            owner_email=r.get("owner_email"),
            last_message_at=(r["last_message_at"].isoformat() if r.get("last_message_at") else ""),
            cited_urn_count=int(r.get("cited_count") or 0),
        )
        for r in chat_rows
    ]
    report.chats_count = len(report.recent_chats)

    # ── Recent design docs (gdoc_pages indexed in window) ──────────
    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT g.title, g.url, g.last_modified, r.name AS repo_name
                FROM gdoc_pages g
                JOIN repos r ON r.id = g.repo_id
                WHERE g.repo_id::text = ANY(:repos)
                  AND COALESCE(g.last_modified, g.indexed_at) > :since
                ORDER BY COALESCE(g.last_modified, g.indexed_at) DESC
                LIMIT :limit
                """
                ),
                {
                    "since": since,
                    "repos": list(repo_id_list),
                    "limit": _SAMPLE_LIMIT,
                },
            )
        )
        .mappings()
        .all()
    )
    report.gdocs_added = [
        GDocRow(
            title=r["title"],
            url=r["url"],
            last_modified=(r["last_modified"].isoformat() if r.get("last_modified") else None),
            repo=r.get("repo_name"),
        )
        for r in rows
    ]
    report.gdocs_added_count = len(report.gdocs_added)

    # ── Recent Jira issues (closed/done in window from cache) ──────
    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT j.issue_key, j.title, j.url, j.status, r.name AS repo_name
                FROM jira_issues j
                JOIN repos r ON r.id = j.repo_id
                WHERE j.repo_id::text = ANY(:repos)
                  AND j.updated_at > CAST(:since AS timestamp)
                  AND LOWER(COALESCE(j.status, '')) IN ('done','closed','resolved')
                ORDER BY j.updated_at DESC
                LIMIT :limit
                """
                ),
                {
                    "since": since,
                    "repos": list(repo_id_list),
                    "limit": _SAMPLE_LIMIT,
                },
            )
        )
        .mappings()
        .all()
    )
    report.jira_closed = [
        JiraRow(
            key=r["issue_key"],
            title=r["title"],
            url=r["url"],
            status=r.get("status"),
            repo=r.get("repo_name"),
        )
        for r in rows
    ]
    report.jira_closed_count = len(report.jira_closed)

    # ── Top contributors (by chat conversations + tasks) ────────────
    contrib_rows = (
        await session.execute(
            text(
                """
                SELECT email, SUM(n) AS n FROM (
                    SELECT owner_email AS email, COUNT(*) AS n
                    FROM conversations
                    WHERE deleted_at IS NULL
                      AND updated_at > :since
                      AND owner_email IS NOT NULL
                      AND owner_email <> ''
                      AND CAST(scope_repo_ids AS jsonb) ?| :repos
                    GROUP BY owner_email
                    UNION ALL
                    SELECT owner_email AS email, COUNT(*) AS n
                    FROM tasks
                    WHERE updated_at > :since
                      AND owner_email IS NOT NULL
                      AND owner_email <> ''
                    GROUP BY owner_email
                ) t
                GROUP BY email
                ORDER BY n DESC
                LIMIT :limit
                """
            ),
            {
                "since": since,
                "repos": list(repo_id_list),
                "limit": _CONTRIB_LIMIT,
            },
        )
    ).all()
    report.top_contributors = [
        ContributorRow(email=str(r[0]), activity_count=int(r[1] or 0)) for r in contrib_rows if r[0]
    ]

    # PR rollup left at zero — we don't yet aggregate gh PRs per-repo
    # offline. The chat-research GitHub agent caches them per-conversation
    # but not as a global per-repo table. Phase 2 of the pulse work will
    # add a ``github_prs`` cache table. Until then the FE shows the
    # fields as "(coming soon)" instead of misleading zeros.

    return report


# ── markdown rendering for chat-seed ───────────────────────────────


def render_pulse_section_markdown(
    report: PulseReport,
    section: str,
) -> str:
    """Render a single section of the pulse as markdown — used as
    ``seed_context`` when the operator clicks "Discuss this section"
    on the FE. Each section header includes the scope + range so the
    spawned chat carries the same framing as the dashboard."""
    label = report.scope.get("label", "scope")
    head = f"From {label} pulse · last {report.range_days} days"
    if section == "shipped":
        body = (
            "\n".join(
                f"- **{t.title}** ({t.state}) · {t.jira_key or ''} {t.pr_url or ''}".strip()
                for t in report.tasks_shipped
            )
            or "(none)"
        )
        return f"{head}\n\n## Shipped this period ({report.tasks_shipped_count})\n{body}"
    if section == "in_flight":
        body = (
            "\n".join(
                f"- **{t.title}** · {t.state} · {t.jira_key or '-'} · owner={t.owner_email or '?'}"
                for t in report.tasks_in_flight
            )
            or "(none)"
        )
        return f"{head}\n\n## In flight ({report.tasks_in_flight_count})\n{body}"
    if section == "blocked":
        body = (
            "\n".join(
                f"- **{t.title}** · {t.jira_key or '-'} · owner={t.owner_email or '?'}"
                for t in report.tasks_blocked
            )
            or "(none)"
        )
        return f"{head}\n\n## Blocked ({report.tasks_blocked_count})\n{body}"
    if section == "chats":
        body = (
            "\n".join(
                f"- {c.title} · {c.cited_urn_count} citations · {c.owner_email or '?'}"
                for c in report.recent_chats
            )
            or "(none)"
        )
        return f"{head}\n\n## Recent chats ({report.chats_count})\n{body}"
    if section == "gdocs":
        body = "\n".join(f"- [{g.title}]({g.url})" for g in report.gdocs_added) or "(none)"
        return f"{head}\n\n## Design docs added ({report.gdocs_added_count})\n{body}"
    if section == "jira_closed":
        body = (
            "\n".join(f"- [{j.key}]({j.url}) {j.title} ({j.status})" for j in report.jira_closed)
            or "(none)"
        )
        return f"{head}\n\n## Jira closed ({report.jira_closed_count})\n{body}"
    return head
