"""Asks Board — auto-routing engine for "I have a question for team X".

The collaboration page's biggest unmet need: an engineer has a
question scoped to some repos, but doesn't know which Slack channel
to post in or who to @-mention. Today they post in
#engineering-help and hope, or DM whoever they last talked to.

This module:
  1. Resolves the owning squad from ``scope_repo_ids`` →
     ``RepoOwnership.{squad,slack_channel,jira_project_key}``.
  2. Drafts a Slack-mrkdwn message via a Haiku call so the operator
     pastes a clean question into the right channel instead of
     re-typing.
  3. Tracks the ask through ``drafted → routed → sent → answered →
     escalated``.

Slack writes are NEVER auto-fired — this module returns the draft;
the operator clicks "Send" in the UI to actually post (today via
copy-paste; future via the slack-send-message-draft skill).

The asker's email comes from the resolved identity principal; the answer (when
it lands) is captured as a chat conversation linked back via
``answer_conversation_id``, closing the loop.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.actions.project_resolver import resolve_for_conversation
from bifrost.cli.config import get_settings

logger = logging.getLogger(__name__)


# ── routing primitives ─────────────────────────────────────────────


@dataclass
class RouteSuggestion:
    target_squad: str | None
    target_slack_channel: str | None
    target_jira_project: str | None
    rationale: str
    candidates: list[dict[str, Any]]
    """All candidate squads (mode + ties) so the UI can show 'we
    matched SHIP; alternatives: INV'."""


async def resolve_route(
    session: AsyncSession,
    *,
    scope_repo_ids: list[str],
) -> RouteSuggestion:
    """Pick the best (slack channel, jira project, squad) tuple for an
    ask scoped to ``scope_repo_ids``. Mode of squad across the repos
    wins; ties surfaced as candidates so the UI prompts.

    Empty scope → empty suggestion (UI surfaces 'no scope picked' and
    forces manual selection)."""
    if not scope_repo_ids:
        return RouteSuggestion(
            target_squad=None,
            target_slack_channel=None,
            target_jira_project=None,
            rationale="no scope_repo_ids provided",
            candidates=[],
        )

    rows = (
        await session.execute(
            text(
                """
                SELECT squad, slack_channel, jira_project_key, COUNT(*) AS n
                FROM repo_ownership
                WHERE repo_id::text = ANY(:repos)
                  AND squad IS NOT NULL
                  AND squad <> ''
                GROUP BY squad, slack_channel, jira_project_key
                ORDER BY n DESC
                """
            ),
            {"repos": list(scope_repo_ids)},
        )
    ).all()
    if not rows:
        return RouteSuggestion(
            target_squad=None,
            target_slack_channel=None,
            target_jira_project=None,
            rationale=(
                "no RepoOwnership rows found for the supplied scope — "
                "add ownership manifest metadata or pick squad manually"
            ),
            candidates=[],
        )

    candidates = [
        {
            "squad": str(r[0]),
            "slack_channel": r[1],
            "jira_project_key": r[2],
            "n": int(r[3]),
        }
        for r in rows
    ]
    top = candidates[0]
    # Tie detection — multiple squads with the same top vote count
    tied = [c for c in candidates if c["n"] == top["n"]]
    if len(tied) > 1:
        return RouteSuggestion(
            target_squad=None,
            target_slack_channel=None,
            target_jira_project=None,
            rationale=(
                f"tied across {len(tied)} squads ({', '.join(c['squad'] for c in tied)}) — pick one"
            ),
            candidates=candidates,
        )
    return RouteSuggestion(
        target_squad=top["squad"],
        target_slack_channel=top.get("slack_channel"),
        target_jira_project=top.get("jira_project_key"),
        rationale=(
            f"matched {top['squad']} squad as the dominant owner "
            f"({top['n']} of {sum(c['n'] for c in candidates)} repos)"
        ),
        candidates=candidates,
    )


# ── Slack draft generation ────────────────────────────────────────


_SLACK_DRAFT_SYSTEM = """\
You draft a single, polite, well-scoped Slack message for an
engineer who needs help from another team. Output is Slack mrkdwn.

Rules:
  * Open with one short sentence stating the ask.
  * Include the squad/channel context so the reader knows why
    they're being pinged.
  * If a Jira project was identified, mention it inline so the
    answerer knows where to file follow-up.
  * Keep it under 140 words. No fluff.
  * NO @here / @channel — these get filtered out by squad mods.
  * If the question is vague, end with a single concrete clarifying
    question instead of a generic "any help appreciated".

Output the message body ONLY — no preamble, no schema, no quotes.
"""


async def draft_slack_message(
    *,
    asker_email: str,
    question: str,
    target_squad: str | None,
    target_jira_project: str | None,
    target_slack_channel: str | None,
    scope_label: str | None = None,
) -> str:
    """Compose the Slack message body via Haiku. Best-effort — caller
    must handle empty / fail case (the UI defaults to the raw
    question so the operator isn't blocked)."""
    settings = get_settings()
    from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient

    bullets = []
    if target_squad:
        bullets.append(f"  - target squad: {target_squad}")
    if target_slack_channel:
        bullets.append(f"  - target channel: {target_slack_channel}")
    if target_jira_project:
        bullets.append(f"  - target Jira project: {target_jira_project}")
    if scope_label:
        bullets.append(f"  - scope: {scope_label}")
    context = "\n".join(bullets) or "  (no auto-resolved context)"

    user_message = (
        f"Asker: {asker_email}\nRouting context:\n{context}\n\nQuestion:\n{question.strip()}\n"
    )
    client = ClaudeLlmClient(settings)
    try:
        completion = await client.call_with_dynamic_max_turns(
            initial_max_turns=1,
            timeout_override=20,
            system_prompt=_SLACK_DRAFT_SYSTEM,
            user_message=user_message,
            model=settings.triage_model,
        )
    except ClaudeCliError as exc:
        logger.info(
            "asks_router.draft_slack: Claude CLI failed (%s); using fallback template",
            exc,
        )
        return _fallback_draft(
            asker_email=asker_email,
            question=question,
            target_squad=target_squad,
            target_jira_project=target_jira_project,
        )
    return (completion.text or "").strip() or _fallback_draft(
        asker_email=asker_email,
        question=question,
        target_squad=target_squad,
        target_jira_project=target_jira_project,
    )


def _fallback_draft(
    *,
    asker_email: str,
    question: str,
    target_squad: str | None,
    target_jira_project: str | None,
) -> str:
    """Template used when Haiku is unavailable. Plain Slack mrkdwn."""
    parts = [
        f"Hi {target_squad or 'team'} — quick question from {asker_email}:",
        "",
        question.strip(),
    ]
    if target_jira_project:
        parts.append("")
        parts.append(
            f"_Happy to file in {target_jira_project} if this turns into work._",
        )
    return "\n".join(parts)


# ── ask CRUD + lifecycle ───────────────────────────────────────────


@dataclass
class AskRow:
    id: str
    asker_email: str
    question: str
    scope_repo_ids: list[str]
    target_squad: str | None
    target_slack_channel: str | None
    target_jira_project: str | None
    slack_draft_md: str | None
    slack_thread_url: str | None
    answer_conversation_id: str | None
    status: str
    created_at: str
    updated_at: str
    answered_at: str | None
    escalated_at: str | None

    @classmethod
    def from_db(cls, r: dict[str, Any]) -> AskRow:
        return cls(
            id=str(r["id"]),
            asker_email=str(r["asker_email"]),
            question=str(r["question"]),
            scope_repo_ids=list(r.get("scope_repo_ids") or []),
            target_squad=r.get("target_squad"),
            target_slack_channel=r.get("target_slack_channel"),
            target_jira_project=r.get("target_jira_project"),
            slack_draft_md=r.get("slack_draft_md"),
            slack_thread_url=r.get("slack_thread_url"),
            answer_conversation_id=(
                str(r["answer_conversation_id"]) if r.get("answer_conversation_id") else None
            ),
            status=str(r["status"]),
            created_at=(r["created_at"].isoformat() if r.get("created_at") else ""),
            updated_at=(r["updated_at"].isoformat() if r.get("updated_at") else ""),
            answered_at=(r["answered_at"].isoformat() if r.get("answered_at") else None),
            escalated_at=(r["escalated_at"].isoformat() if r.get("escalated_at") else None),
        )


async def create_ask(
    session: AsyncSession,
    *,
    asker_email: str,
    question: str,
    scope_repo_ids: list[str],
    target_squad_override: str | None = None,
) -> str:
    """Insert a new ask row in DRAFTED state with auto-resolved
    routing + Slack draft. Returns the new ask id.

    ``target_squad_override`` lets the FE force a specific squad
    when auto-resolution is ambiguous (the UI shows the picker)."""
    ask_id = str(uuid.uuid4())
    route = await resolve_route(session, scope_repo_ids=scope_repo_ids)
    if target_squad_override:
        # Find the channel + jira project for the override squad
        row = (
            await session.execute(
                text(
                    "SELECT slack_channel, jira_project_key "
                    "FROM repo_ownership "
                    "WHERE LOWER(squad) = LOWER(:s) "
                    "  AND slack_channel IS NOT NULL "
                    "LIMIT 1"
                ),
                {"s": target_squad_override},
            )
        ).first()
        slack_channel = row[0] if row else None
        jira_project = row[1] if row else None
        target_squad = target_squad_override
    else:
        target_squad = route.target_squad
        slack_channel = route.target_slack_channel
        jira_project = route.target_jira_project

    # Best-effort Slack draft. If Haiku is down we still store an
    # empty draft so the UI can render the row + the operator can
    # type their own.
    draft = ""
    if target_squad:
        try:
            draft = await draft_slack_message(
                asker_email=asker_email,
                question=question,
                target_squad=target_squad,
                target_jira_project=jira_project,
                target_slack_channel=slack_channel,
            )
        except Exception:  # noqa: BLE001
            logger.exception("asks_router: draft_slack failed; storing empty")

    initial_status = "drafted" if not target_squad else "routed"

    await session.execute(
        text(
            """
            INSERT INTO collab_asks (
                id, asker_email, question, scope_repo_ids,
                target_squad, target_slack_channel,
                target_jira_project, slack_draft_md, status
            )
            VALUES (
                CAST(:id AS uuid), :ae, :q, CAST(:srids AS JSONB),
                :ts, :tsc, :tjp, :draft, :status
            )
            """
        ),
        {
            "id": ask_id,
            "ae": asker_email,
            "q": question,
            "srids": json.dumps(list(scope_repo_ids)),
            "ts": target_squad,
            "tsc": slack_channel,
            "tjp": jira_project,
            "draft": draft or None,
            "status": initial_status,
        },
    )
    return ask_id


async def list_asks(
    session: AsyncSession,
    *,
    asker_email: str | None = None,
    target_squad: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[AskRow]:
    """List asks with optional filters. Newest first."""
    where: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if asker_email:
        where.append("asker_email = :ae")
        params["ae"] = asker_email
    if target_squad:
        where.append("LOWER(target_squad) = LOWER(:ts)")
        params["ts"] = target_squad
    if status == "open":
        where.append("status NOT IN ('answered','cancelled')")
    elif status:
        where.append("status = :st")
        params["st"] = status
    sql = "SELECT * FROM collab_asks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT :limit"
    rows = (await session.execute(text(sql), params)).mappings().all()
    return [AskRow.from_db(dict(r)) for r in rows]


async def get_ask(
    session: AsyncSession,
    *,
    ask_id: str,
) -> AskRow | None:
    row = (
        (
            await session.execute(
                text("SELECT * FROM collab_asks WHERE id = CAST(:id AS uuid)"),
                {"id": ask_id},
            )
        )
        .mappings()
        .first()
    )
    return AskRow.from_db(dict(row)) if row else None


async def mark_sent(
    session: AsyncSession,
    *,
    ask_id: str,
    slack_thread_url: str | None,
) -> None:
    """Operator clicked 'Sent to Slack' — record the thread URL so
    the answer-tracking back-link can render."""
    await session.execute(
        text(
            "UPDATE collab_asks "
            "SET status = 'sent', slack_thread_url = :url, "
            "    updated_at = NOW() "
            "WHERE id = CAST(:id AS uuid)"
        ),
        {"id": ask_id, "url": slack_thread_url},
    )


async def mark_answered(
    session: AsyncSession,
    *,
    ask_id: str,
    answer_conversation_id: str | None = None,
) -> None:
    """The ask got answered — typically because a chat conversation
    was spawned via 'Discuss' and produced an answer."""
    # Postgres can't infer the type of a NULLable text parameter
    # used in CASE WHEN without an explicit cast — pre-cast to text
    # so the resulting CAST(... AS uuid) inside the CASE is
    # unambiguous.
    await session.execute(
        text(
            "UPDATE collab_asks "
            "SET status = 'answered', "
            "    answer_conversation_id = "
            "      CASE WHEN CAST(:acid AS text) IS NULL "
            "           THEN answer_conversation_id "
            "           ELSE CAST(:acid AS uuid) END, "
            "    answered_at = NOW(), "
            "    updated_at = NOW() "
            "WHERE id = CAST(:id AS uuid)"
        ),
        {"id": ask_id, "acid": answer_conversation_id},
    )


async def mark_escalated(
    session: AsyncSession,
    *,
    ask_id: str,
) -> None:
    """48h+ no reply — operator escalates so it bumps to the top of
    the board with a different colour."""
    await session.execute(
        text(
            "UPDATE collab_asks "
            "SET status = 'escalated', "
            "    escalated_at = NOW(), "
            "    updated_at = NOW() "
            "WHERE id = CAST(:id AS uuid)"
        ),
        {"id": ask_id},
    )


async def cancel_ask(
    session: AsyncSession,
    *,
    ask_id: str,
) -> None:
    await session.execute(
        text(
            "UPDATE collab_asks "
            "SET status = 'cancelled', updated_at = NOW() "
            "WHERE id = CAST(:id AS uuid)"
        ),
        {"id": ask_id},
    )


# Re-export the project_resolver for tests that don't import this layer
__all__ = [
    "AskRow",
    "RouteSuggestion",
    "cancel_ask",
    "create_ask",
    "draft_slack_message",
    "get_ask",
    "list_asks",
    "mark_answered",
    "mark_escalated",
    "mark_sent",
    "resolve_route",
    "resolve_for_conversation",
]
