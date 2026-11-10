"""Resolve the right Jira project for a Bifrost task.

The chat→task→Jira flow needs to pick the right Atlassian project
WITHOUT prompting the user every time. Resolution order:

  1. **Explicit override** — caller passed ``project_key`` directly
     (the spawn modal's project picker takes precedence).

  2. **Existing jira_key on the task** — the task is already linked
     to a Jira issue; extract the project from the key prefix
     (``SHIP-1234`` → ``SHIP``).

  3. **Conversation scope** — chat-spawned tasks carry a
     ``conversation_id``. Look up the conversation's
     ``scope_repo_ids`` → ``RepoOwnership.jira_project_key`` for each
     → take the dominant key. Tie → return ``None`` so the caller
     surfaces the picker.

  4. **No signal** — return ``None``. The route layer is responsible
     for prompting the user (the spawn modal supplies the picker UI;
     non-modal callers can refuse to write to Jira).

Why this is a separate module: the same resolution is needed in
multiple places — spawn modal preview, lifecycle hook firing for
existing tasks that lack a project, reverse sync, retract. Centralised
so the heuristic is consistent.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedProject:
    """Outcome of a project lookup."""

    project_key: str
    source: str  # "explicit" | "jira_key" | "scope" | "user_pick"
    candidates: tuple[str, ...] = ()  # alternative keys (for tie-break UI)
    reason: str = ""


# ── public API ──────────────────────────────────────────────────────


def project_from_jira_key(jira_key: str | None) -> str | None:
    """Extract the project prefix from a Jira issue key.

    ``SHIP-1234`` → ``SHIP``. Returns ``None`` if the key doesn't
    match the canonical ``<PROJECT>-<NUM>`` shape — defensive against
    malformed keys hand-typed by the user."""
    if not jira_key:
        return None
    parts = jira_key.strip().split("-", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    prefix = parts[0]
    # Real Jira project keys are alphanumeric AND must start with an
    # alpha char. ``123-456`` is not a valid Jira key — reject it.
    if not prefix.isalnum() or not prefix[:1].isalpha():
        return None
    return prefix.upper()


async def resolve_for_task(
    session: AsyncSession,
    *,
    task_id: str,
    explicit: str | None = None,
) -> ResolvedProject | None:
    """Resolve the Jira project for an existing task.

    Hits the DB to read the task's ``jira_key`` and the originating
    conversation's ``scope_repo_ids`` (if any). Returns ``None`` only
    when no signal is available — caller MUST handle that branch
    (typically by prompting the user)."""
    if explicit:
        return ResolvedProject(
            project_key=explicit.upper(),
            source="explicit",
            reason="caller-supplied project_key",
        )

    row = (
        await session.execute(
            text("SELECT jira_key, conversation_id FROM tasks WHERE id = :id"),
            {"id": task_id},
        )
    ).first()
    if row is None:
        return None
    jira_key, conv_id = row

    derived = project_from_jira_key(jira_key)
    if derived:
        return ResolvedProject(
            project_key=derived,
            source="jira_key",
            reason=f"derived from existing jira_key={jira_key}",
        )

    if conv_id:
        return await resolve_for_conversation(
            session,
            conversation_id=str(conv_id),
        )
    return None


async def resolve_for_conversation(
    session: AsyncSession,
    *,
    conversation_id: str,
) -> ResolvedProject | None:
    """Resolve the project from a conversation's repo scope.

    Picks the dominant ``RepoOwnership.jira_project_key`` across the
    conversation's ``scope_repo_ids``. Ties (two projects with the
    same vote count) return ``None`` with the candidates surfaced
    via a separate query — caller decides how to present the choice.
    """
    conv_row = (
        await session.execute(
            text("SELECT scope_repo_ids FROM conversations WHERE id = :id"),
            {"id": conversation_id},
        )
    ).first()
    if conv_row is None:
        return None
    raw_ids = conv_row[0] or []
    if not raw_ids:
        return None

    rows = (
        await session.execute(
            text(
                "SELECT jira_project_key "
                "FROM repo_ownership "
                "WHERE repo_id = ANY(CAST(:ids AS uuid[])) "
                "  AND jira_project_key IS NOT NULL "
                "  AND jira_project_key <> ''"
            ),
            {"ids": list(raw_ids)},
        )
    ).all()
    keys = [str(r[0]).upper() for r in rows if r[0]]
    if not keys:
        return None

    counts = Counter(keys)
    most_common = counts.most_common()
    top_key, top_count = most_common[0]
    candidates = tuple(k for k, _ in most_common)

    # Tie-break: if 2+ projects have the SAME top vote count, leave
    # the choice to the user. Otherwise the dominant key wins.
    tied = [k for k, c in most_common if c == top_count]
    if len(tied) > 1:
        logger.info(
            "project_resolver: tie %s for conversation=%s; deferring to picker",
            tied,
            conversation_id,
        )
        return ResolvedProject(
            project_key="",  # empty signals "ambiguous"
            source="scope_tied",
            candidates=candidates,
            reason=f"tied across {len(tied)} projects: {', '.join(tied)}",
        )

    return ResolvedProject(
        project_key=top_key,
        source="scope",
        candidates=candidates,
        reason=(f"dominant project across {sum(counts.values())} repo(s): {top_count}× {top_key}"),
    )


async def all_known_projects(session: AsyncSession) -> list[str]:
    """List every distinct ``jira_project_key`` known to ``repo_ownership``.

    Powers the spawn modal's project picker dropdown when no project
    can be resolved from scope (the user picks from the full list).
    Sorted by repo count (popular projects first)."""
    rows = (
        await session.execute(
            text(
                "SELECT jira_project_key, COUNT(*) AS n "
                "FROM repo_ownership "
                "WHERE jira_project_key IS NOT NULL "
                "  AND jira_project_key <> '' "
                "GROUP BY jira_project_key "
                "ORDER BY n DESC, jira_project_key ASC"
            )
        )
    ).all()
    return [str(r[0]).upper() for r in rows if r[0]]
