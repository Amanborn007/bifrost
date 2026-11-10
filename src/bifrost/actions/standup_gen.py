"""Standup Generator — weekly activity → Slack-mrkdwn draft.

Friday afternoon, every engineer writes a weekly status
update. The friction is collation: piecing together "what did I do"
from Jira, GitHub, Slack, Confluence by hand. Bifrost has all of
that already (tasks I owned + PRs I authored + chats I had + Jira I
moved); the standup is just an aggregate.

This module produces a markdown draft with three sections:

  Done this week:
    • <task_title> · PROJ-1234 · PR 4242 (merged)
    • <chat title> · linked task PROJ-9999
  Working on:
    • <task_title> · executing · PROJ-2222
  Blocked / asks:
    • <task_title> · blocked: vendor outage
    • Open ask routed to SHIP: "Why does X..." · 2d ago

The output is intended for paste-into-Slack — mrkdwn-compatible, no
fancy structure, no emojis.

V1 returns the markdown for manual paste; the slack-send-message-draft
skill orchestration is a future "Send to #my-standup" button.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


@dataclass
class StandupDraft:
    email: str
    range_days: int = 7
    generated_at: str = ""
    markdown: str = ""

    # Structured rollup so the FE can render sectioned UI alongside
    # the plain mrkdwn (toggle between "render" and "raw markdown").
    done: list[dict] = field(default_factory=list)
    in_flight: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    open_asks: list[dict] = field(default_factory=list)
    jira_items: list[dict] = field(default_factory=list)


async def build_standup(
    session: AsyncSession,
    *,
    email: str,
    range_days: int = 7,
) -> StandupDraft:
    since = datetime.utcnow() - timedelta(days=range_days)
    draft = StandupDraft(
        email=email,
        range_days=range_days,
        generated_at=datetime.now(UTC).isoformat(),
    )

    # ── DONE: tasks pr_merged this week ────────────────────────────
    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT id::text AS id, title, state, jira_key,
                       pr_url, pr_number, updated_at
                FROM tasks
                WHERE owner_email = :email
                  AND state = 'pr_merged'
                  AND updated_at > :since
                ORDER BY updated_at DESC
                """
                ),
                {"email": email, "since": since},
            )
        )
        .mappings()
        .all()
    )
    draft.done = [
        {
            "kind": "task_merged",
            "title": str(r["title"] or ""),
            "jira_key": r.get("jira_key"),
            "pr_url": r.get("pr_url"),
            "pr_number": r.get("pr_number"),
            "updated_at": (r["updated_at"].isoformat() if r.get("updated_at") else ""),
        }
        for r in rows
    ]

    # ── IN-FLIGHT: tasks I own actively progressing ────────────────
    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT id::text AS id, title, state, jira_key, pr_url,
                       pr_number, updated_at
                FROM tasks
                WHERE owner_email = :email
                  AND state IN ('plan_ready','executing','verifying',
                                'success','drafting_plan','discussing')
                ORDER BY updated_at DESC
                LIMIT 20
                """
                ),
                {"email": email},
            )
        )
        .mappings()
        .all()
    )
    draft.in_flight = [
        {
            "kind": "task_in_flight",
            "title": str(r["title"] or ""),
            "state": str(r["state"]),
            "jira_key": r.get("jira_key"),
            "pr_url": r.get("pr_url"),
        }
        for r in rows
    ]

    # ── BLOCKED ────────────────────────────────────────────────────
    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT id::text AS id, title, state, jira_key
                FROM tasks
                WHERE owner_email = :email
                  AND state = 'blocked'
                ORDER BY updated_at DESC
                LIMIT 20
                """
                ),
                {"email": email},
            )
        )
        .mappings()
        .all()
    )
    draft.blocked = [
        {
            "kind": "task_blocked",
            "title": str(r["title"] or ""),
            "jira_key": r.get("jira_key"),
        }
        for r in rows
    ]

    # ── OPEN ASKS (mine, not yet answered) ─────────────────────────
    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT id::text, question, target_squad, status,
                       created_at
                FROM collab_asks
                WHERE asker_email = :email
                  AND status NOT IN ('answered','cancelled')
                ORDER BY created_at DESC
                LIMIT 10
                """
                ),
                {"email": email},
            )
        )
        .mappings()
        .all()
    )
    draft.open_asks = [
        {
            "kind": "open_ask",
            "id": str(r["id"]) if r.get("id") else None,
            "question": str(r["question"] or "")[:160],
            "target_squad": r.get("target_squad"),
            "status": str(r["status"]),
            "created_at": (r["created_at"].isoformat() if r.get("created_at") else ""),
        }
        for r in rows
    ]

    # ── JIRA ACTIVITY: recently-updated issues from synced projects ───
    # tasks.repo_id does not exist, so we cannot filter by repos the
    # user owns tasks for. Use the external_sync_log fallback only:
    # any Jira project synced in the last 7 days.
    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT ji.issue_key, ji.title, ji.status, ji.issue_type,
                       ji.url, ji.updated_at, r.name AS repo_name
                FROM jira_issues ji
                JOIN repos r ON r.id = ji.repo_id
                WHERE ji.updated_at > :since
                  AND ji.repo_id IN (
                    SELECT repo_id FROM external_sync_log
                    WHERE source = 'jira'
                      AND last_synced_at > NOW() - INTERVAL '7 days'
                  )
                ORDER BY ji.updated_at DESC
                LIMIT 20
                """
                ),
                {"since": since},
            )
        )
        .mappings()
        .all()
    )
    draft.jira_items = [
        {
            "kind": "jira_issue",
            "issue_key": str(r["issue_key"] or ""),
            "title": str(r["title"] or ""),
            "status": str(r["status"] or ""),
            "issue_type": r.get("issue_type"),
            "url": r.get("url"),
            "repo_name": r.get("repo_name"),
            "updated_at": (r["updated_at"].isoformat() if r.get("updated_at") else ""),
        }
        for r in rows
    ]

    draft.markdown = _render_markdown(draft)
    return draft


def _render_markdown(d: StandupDraft) -> str:
    """Render the rollup as Slack-mrkdwn for paste-into-channel.

    Slack mrkdwn (NOT GFM): single asterisks for bold, no list-marker
    sensitivity (a leading bullet ``• `` works fine), URLs render
    inline. Avoid headings (``#``) — Slack ignores them — use bold
    section titles instead."""
    parts: list[str] = [
        f"*Standup — last {d.range_days} days*",
        "",
    ]

    if d.done:
        parts.append("*Done:*")
        for item in d.done:
            line = f"• {item['title']}"
            extras = []
            if item.get("jira_key"):
                extras.append(item["jira_key"])
            if item.get("pr_number"):
                pr = f"PR #{item['pr_number']}" + (
                    f" ({item['pr_url']})" if item.get("pr_url") else ""
                )
                extras.append(pr)
            if extras:
                line += " · " + " · ".join(extras)
            parts.append(line)
        parts.append("")
    else:
        parts.append("*Done:* (nothing merged this week)")
        parts.append("")

    if d.in_flight:
        parts.append("*Working on:*")
        for item in d.in_flight:
            line = f"• {item['title']} · {item['state']}"
            if item.get("jira_key"):
                line += f" · {item['jira_key']}"
            parts.append(line)
        parts.append("")

    if d.blocked:
        parts.append("*Blocked:*")
        for item in d.blocked:
            line = f"• {item['title']}"
            if item.get("jira_key"):
                line += f" · {item['jira_key']}"
            parts.append(line)
        parts.append("")

    if d.open_asks:
        parts.append("*Open asks:*")
        for ask in d.open_asks:
            line = f"• {ask['question']}"
            if ask.get("target_squad"):
                line += f" · routed to {ask['target_squad']}"
            line += f" · {ask['status']}"
            parts.append(line)
        parts.append("")

    if d.jira_items:
        parts.append(f"*Jira activity (last {d.range_days} days):*")
        for item in d.jira_items:
            line = f"• {item['issue_key']} · {item['title']} · {item['status']}"
            parts.append(line)
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"
