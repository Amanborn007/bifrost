"""Personal inbox aggregator — "what's mine to do right now".

The single source of truth for the operator's actionable surface
across the whole product. Drives:

  * The dashboard hero (InboxPanel) — top of /overview
  * The sidebar's per-nav badge counts
  * The Cmd+K palette's recents

Five sections, ranked by urgency:

  1. ESCALATED   — the user's asks that bumped to escalated state
  2. BLOCKED     — the user's tasks in BLOCKED state
  3. AWAITING_ME — asks routed to the user's squad that aren't answered yet
  4. IN_REVIEW   — the user's PRs (state=success)
  5. OPEN_WORK   — the user's tasks in any active non-terminal state

Each row carries enough context (state, jira_key, pr_url, target_squad)
to render a one-line summary without further fetches.

Pure read aggregation — no LLM, no MCP. Reads from `tasks`,
`collab_asks`, `repo_ownership`. Counts are returned alongside the
sample rows so the FE can show "+N more" badges + sidebar badges.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.cli.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class InboxTask:
    id: str
    title: str
    state: str
    jira_key: str | None
    pr_url: str | None
    pr_number: int | None
    updated_at: str
    jira_url: str | None = None


@dataclass
class InboxAsk:
    id: str
    question: str
    asker_email: str
    target_squad: str | None
    target_slack_channel: str | None
    status: str
    created_at: str


@dataclass
class InboxReviewPR:
    pr_url: str
    pr_number: int | None
    title: str
    task_id: str | None
    task_title: str | None
    rationale: str = ""


@dataclass
class InboxCounts:
    """Sidebar-badge friendly counts. Matches the section sample
    arrays: ``escalated`` is len(escalated_asks), etc. Surfaced
    separately so a 0-row section renders the right "all clear"
    affordance instead of looking broken."""

    escalated: int = 0
    blocked: int = 0
    awaiting_me: int = 0
    in_review: int = 0
    open_work: int = 0
    total_actionable: int = 0


@dataclass
class InboxReport:
    email: str
    generated_at: str
    counts: InboxCounts = field(default_factory=InboxCounts)
    escalated_asks: list[InboxAsk] = field(default_factory=list)
    blocked_tasks: list[InboxTask] = field(default_factory=list)
    awaiting_my_squad: list[InboxAsk] = field(default_factory=list)
    my_prs_in_review: list[InboxReviewPR] = field(default_factory=list)
    open_work: list[InboxTask] = field(default_factory=list)


_LIMIT_PER_SECTION = 10


async def build_inbox(
    session: AsyncSession,
    *,
    email: str,
) -> InboxReport:
    """Compose the inbox for ``email``. Each section caps at
    ``_LIMIT_PER_SECTION`` rows; full counts live on
    ``counts.<section>``."""
    report = InboxReport(
        email=email,
        generated_at=datetime.utcnow().isoformat(),
    )

    # ── 1) Escalated asks I posted ─────────────────────────────────
    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT id::text, question, asker_email, target_squad,
                       target_slack_channel, status, created_at
                FROM collab_asks
                WHERE asker_email = :email
                  AND status = 'escalated'
                ORDER BY escalated_at DESC NULLS LAST, created_at DESC
                LIMIT :lim
                """
                ),
                {"email": email, "lim": _LIMIT_PER_SECTION},
            )
        )
        .mappings()
        .all()
    )
    report.escalated_asks = [
        InboxAsk(
            id=str(r["id"]),
            question=str(r["question"])[:200],
            asker_email=str(r["asker_email"]),
            target_squad=r.get("target_squad"),
            target_slack_channel=r.get("target_slack_channel"),
            status=str(r["status"]),
            created_at=(r["created_at"].isoformat() if r.get("created_at") else ""),
        )
        for r in rows
    ]
    report.counts.escalated = len(report.escalated_asks)

    # ── 2) Blocked tasks I own ─────────────────────────────────────
    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT id::text, title, state, jira_key, pr_url,
                       pr_number, updated_at
                FROM tasks
                WHERE owner_email = :email
                  AND state = 'blocked'
                ORDER BY updated_at DESC
                LIMIT :lim
                """
                ),
                {"email": email, "lim": _LIMIT_PER_SECTION},
            )
        )
        .mappings()
        .all()
    )
    report.blocked_tasks = [_task_row(r) for r in rows]
    report.counts.blocked = len(report.blocked_tasks)

    # ── 3) Asks routed to the user's squad(s) (still unanswered) ───

    # Squads come from tasks + conversations + the email prefix match;
    # when none resolves the section is skipped (identity groups → squads
    # is not wired yet).
    my_squads = await _my_squads(session, email=email)
    if my_squads:
        rows = (
            (
                await session.execute(
                    text(
                        """
                    SELECT id::text, question, asker_email,
                           target_squad, target_slack_channel,
                           status, created_at
                    FROM collab_asks
                    WHERE LOWER(target_squad) = ANY(:squads)
                      AND status IN ('routed', 'sent')
                      AND asker_email <> :email
                    ORDER BY created_at DESC
                    LIMIT :lim
                    """
                    ),
                    {
                        "squads": [s.lower() for s in my_squads],
                        "email": email,
                        "lim": _LIMIT_PER_SECTION,
                    },
                )
            )
            .mappings()
            .all()
        )
        report.awaiting_my_squad = [
            InboxAsk(
                id=str(r["id"]),
                question=str(r["question"])[:200],
                asker_email=str(r["asker_email"]),
                target_squad=r.get("target_squad"),
                target_slack_channel=r.get("target_slack_channel"),
                status=str(r["status"]),
                created_at=(r["created_at"].isoformat() if r.get("created_at") else ""),
            )
            for r in rows
        ]
    report.counts.awaiting_me = len(report.awaiting_my_squad)

    # ── 4) My PRs awaiting review ──────────────────────────────────
    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT id::text, title, state, jira_key, pr_url,
                       pr_number
                FROM tasks
                WHERE owner_email = :email
                  AND state = 'success'
                  AND pr_url IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT :lim
                """
                ),
                {"email": email, "lim": _LIMIT_PER_SECTION},
            )
        )
        .mappings()
        .all()
    )
    report.my_prs_in_review = [
        InboxReviewPR(
            pr_url=str(r["pr_url"]),
            pr_number=int(r["pr_number"]) if r.get("pr_number") else None,
            title=str(r.get("title") or ""),
            task_id=str(r["id"]),
            task_title=str(r.get("title") or ""),
            rationale=(
                "Your task — awaiting review" + (f" · {r['jira_key']}" if r.get("jira_key") else "")
            ),
        )
        for r in rows
    ]
    report.counts.in_review = len(report.my_prs_in_review)

    # ── 5) Open work — tasks I own in any active state ─────────────
    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT id::text, title, state, jira_key, pr_url,
                       pr_number, updated_at
                FROM tasks
                WHERE owner_email = :email
                  AND state IN ('discussing','discuss_done',
                                'drafting_plan','plan_ready',
                                'executing','verifying')
                ORDER BY updated_at DESC
                LIMIT :lim
                """
                ),
                {"email": email, "lim": _LIMIT_PER_SECTION},
            )
        )
        .mappings()
        .all()
    )
    report.open_work = [_task_row(r) for r in rows]
    report.counts.open_work = len(report.open_work)

    report.counts.total_actionable = (
        report.counts.escalated
        + report.counts.blocked
        + report.counts.awaiting_me
        + report.counts.in_review
        + report.counts.open_work
    )
    return report


def _task_row(r: dict[str, Any]) -> InboxTask:
    return InboxTask(
        id=str(r["id"]),
        title=str(r.get("title") or ""),
        state=str(r["state"]),
        jira_key=r.get("jira_key"),
        pr_url=r.get("pr_url"),
        pr_number=int(r["pr_number"]) if r.get("pr_number") else None,
        updated_at=(r["updated_at"].isoformat() if r.get("updated_at") else ""),
        jira_url=get_settings().sources.jira.browse_url(r.get("jira_key")),
    )


async def _my_squads(
    session: AsyncSession,
    *,
    email: str,
) -> list[str]:
    """Heuristic — derive the user's squads from squads of repos appearing
    in tasks they own AND conversations they started. Falls back to empty
    when no signal exists yet (the 'awaiting_my_squad' section just
    stays empty).

    Phase 2: replace with identity-groups → squads mapping when we have
    that wiring."""
    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT o.squad
                FROM repo_ownership o
                WHERE o.squad IS NOT NULL
                  AND o.repo_id IN (
                      -- repos referenced by my tasks (via conv scope)
                      SELECT CAST(jsonb_array_elements_text(c.scope_repo_ids) AS uuid)
                      FROM tasks t
                      JOIN conversations c ON c.id = t.conversation_id
                      WHERE t.owner_email = :email
                  )
                LIMIT 20
                """
            ),
            {"email": email},
        )
    ).all()
    return [str(r[0]) for r in rows if r[0]]
