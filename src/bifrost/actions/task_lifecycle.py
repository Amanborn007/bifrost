"""Task lifecycle state machine — authoritative source for legal
transitions. Every state change writes a `task_events` audit row.

States:
    draft_intent       — just created; intent captured, nothing else
    discussing         — discussion agent active in current phase
    discuss_done       — context.md approved for current phase
    drafting_plan      — planner/plan-checker active
    plan_ready         — plan.md approved
    executing          — per-plan execution in worktree
    verifying          — conversational acceptance testing
    success            — PR open, all verify items pass
    blocked            — verify fail OR agent/extraction budget exceeded
    cancelled          — user abort; worktree deleted

Phases live inside a task and run the 4-step loop
(discuss → plan → execute → verify). The task's top-level state is a
rollup of the current phase's position plus task-wide flags.
"""

from __future__ import annotations

import enum
import uuid as _uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class TaskState(enum.StrEnum):
    DRAFT_INTENT = "draft_intent"
    DISCUSSING = "discussing"
    DISCUSS_DONE = "discuss_done"
    DRAFTING_PLAN = "drafting_plan"
    PLAN_READY = "plan_ready"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    SUCCESS = "success"  # PR opened, awaiting review
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    PR_MERGED = "pr_merged"  # PR merged → terminal SDLC done


class PhaseStep(enum.StrEnum):
    DISCUSS = "discuss"
    PLAN = "plan"
    EXECUTE = "execute"
    VERIFY = "verify"
    DONE = "done"


# Legal forward transitions (every other edge must route through
# CANCELLED for safety). Additionally BLOCKED is a sink that recovers
# via explicit user intervention (unblock → EXECUTING).
_LEGAL_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.DRAFT_INTENT: {TaskState.DISCUSSING, TaskState.CANCELLED},
    TaskState.DISCUSSING: {TaskState.DISCUSS_DONE, TaskState.CANCELLED, TaskState.BLOCKED},
    TaskState.DISCUSS_DONE: {TaskState.DRAFTING_PLAN, TaskState.DISCUSSING, TaskState.CANCELLED},
    TaskState.DRAFTING_PLAN: {
        TaskState.PLAN_READY,
        TaskState.DISCUSSING,
        TaskState.CANCELLED,
        TaskState.BLOCKED,
    },
    TaskState.PLAN_READY: {TaskState.EXECUTING, TaskState.DRAFTING_PLAN, TaskState.CANCELLED},
    TaskState.EXECUTING: {TaskState.VERIFYING, TaskState.BLOCKED, TaskState.CANCELLED},
    TaskState.VERIFYING: {
        TaskState.SUCCESS,
        TaskState.BLOCKED,
        TaskState.EXECUTING,
        TaskState.CANCELLED,
    },
    TaskState.SUCCESS: {TaskState.PR_MERGED, TaskState.CANCELLED},  # PR open → merged, or abandon
    TaskState.BLOCKED: {
        TaskState.EXECUTING,
        TaskState.DRAFTING_PLAN,
        TaskState.DISCUSSING,
        TaskState.CANCELLED,
    },
    TaskState.CANCELLED: set(),  # terminal
    TaskState.PR_MERGED: set(),  # terminal SDLC complete
}


class IllegalTransition(Exception):
    pass


@dataclass
class TransitionResult:
    old_state: TaskState
    new_state: TaskState
    event_id: str


async def record_event(
    session: AsyncSession,
    *,
    task_id: str,
    actor: str,  # "user" | "agent:<kind>" | "system"
    action: str,  # "create" | "transition" | ...
    payload: dict[str, Any] | None = None,
) -> str:
    event_id = str(_uuid.uuid4())
    await session.execute(
        text(
            """
            INSERT INTO task_events (id, task_id, actor, action, payload, at)
            VALUES (:id, :task_id, :actor, :action, CAST(:payload AS JSONB), :at)
            """
        ),
        {
            "id": event_id,
            "task_id": task_id,
            "actor": actor,
            "action": action,
            "payload": _dump_json(payload or {}),
            "at": datetime.now(UTC),
        },
    )
    return event_id


async def transition(
    session: AsyncSession,
    *,
    task_id: str,
    to_state: TaskState,
    actor: str,
    reason: str = "",
    fire_jira_hook: bool = True,
) -> TransitionResult:
    """Move a task to a new state, writing an audit event.

    Raises IllegalTransition if the move isn't in _LEGAL_TRANSITIONS.

    Side effect: fires the Jira sync hook AFTER the transition has
    been committed (the hook runs in a fresh session). The hook
    creates / transitions / comments on the linked Jira issue per
    ``actions.jira_state_map``. Failures in the hook are logged but
    NEVER roll back the transition — Jira is a projection of Bifrost
    state, not its source of truth. Pass ``fire_jira_hook=False`` to
    suppress (used by reverse-sync where Jira state already drove the
    Bifrost transition).
    """
    row = (
        await session.execute(
            text("SELECT state FROM tasks WHERE id = :id FOR UPDATE"),
            {"id": task_id},
        )
    ).first()
    if row is None:
        raise ValueError(f"task {task_id} not found")
    old_state = TaskState(row[0])

    if to_state not in _LEGAL_TRANSITIONS.get(old_state, set()):
        raise IllegalTransition(f"{old_state.value} → {to_state.value} not allowed")

    await session.execute(
        text("UPDATE tasks SET state = :s, updated_at = :at WHERE id = :id"),
        {
            "s": to_state.value,
            "at": datetime.now(UTC),
            "id": task_id,
        },
    )
    event_id = await record_event(
        session,
        task_id=task_id,
        actor=actor,
        action="transition",
        payload={
            "from": old_state.value,
            "to": to_state.value,
            "reason": reason,
        },
    )

    if fire_jira_hook:
        # Schedule the hook AFTER the calling session commits. We do
        # NOT await it here because:
        #  (a) the caller's session may not have committed yet — the
        #      hook reads the task in a NEW session and would race;
        #  (b) Jira writes are 3-5s of latency we don't want to block
        #      the transition response on.
        # Solution: stash the (task_id, old, new, reason) tuple on
        # the session object via a pending-callback list. The route
        # layer drains the list AFTER ``session.commit()`` and fires
        # each callback as an asyncio.create_task.
        _enqueue_post_commit_jira_hook(
            session,
            task_id=task_id,
            old_state=old_state,
            new_state=to_state,
            reason=reason,
            actor=actor,
        )

    return TransitionResult(old_state=old_state, new_state=to_state, event_id=event_id)


# ── post-commit Jira hook queue ─────────────────────────────────────


_PENDING_HOOKS_ATTR = "_bifrost_pending_jira_hooks"


def _enqueue_post_commit_jira_hook(
    session: AsyncSession,
    *,
    task_id: str,
    old_state: TaskState,
    new_state: TaskState,
    reason: str,
    actor: str,
) -> None:
    """Stash a (task, transition) tuple on the session so the route
    handler can fire the Jira hook AFTER it commits."""
    pending = getattr(session, _PENDING_HOOKS_ATTR, None)
    if pending is None:
        pending = []
        setattr(session, _PENDING_HOOKS_ATTR, pending)
    pending.append(
        {
            "task_id": task_id,
            "old_state": old_state,
            "new_state": new_state,
            "reason": reason,
            "actor": actor,
        }
    )


async def fire_pending_jira_hooks(
    session: AsyncSession,
    *,
    session_factory: Any,
) -> None:
    """Drain the pending hooks queue on a session and fire each.

    Call AFTER ``session.commit()`` so the underlying transition is
    durable before the Jira write fires. The hook itself opens a new
    session via ``session_factory`` so this function can be called
    even while the original session is still alive."""
    pending = getattr(session, _PENDING_HOOKS_ATTR, None)
    if not pending:
        return
    setattr(session, _PENDING_HOOKS_ATTR, [])

    import asyncio

    from bifrost.actions.jira_sync import on_transition

    for hook in pending:
        # Fire-and-forget — we don't want lifecycle endpoints to wait
        # 3-5s on Jira MCP. The hook itself has its own timeout +
        # error handling.
        asyncio.create_task(
            on_transition(
                session_factory,
                task_id=hook["task_id"],
                old_state=hook["old_state"],
                new_state=hook["new_state"],
                reason=hook["reason"],
                actor=hook["actor"],
            ),
        )


def _dump_json(obj: Any) -> str:
    import json

    return json.dumps(obj, default=str)


async def create_task(
    session: AsyncSession,
    *,
    title: str,
    intent_text: str,
    created_from: str = "direct",  # chat | jira | direct
    conversation_id: str | None = None,
    jira_key: str | None = None,
) -> str:
    """Insert a new task in DRAFT_INTENT state; returns the task id."""
    task_id = str(_uuid.uuid4())
    now = datetime.now(UTC)
    await session.execute(
        text(
            """
            INSERT INTO tasks (
                id, title, state, created_from, conversation_id,
                jira_key, intent_text, created_at, updated_at
            )
            VALUES (
                :id, :title, 'draft_intent', :created_from, :conv_id,
                :jira_key, :intent_text, :now, :now
            )
            """
        ),
        {
            "id": task_id,
            "title": title[:200],
            "created_from": created_from,
            "conv_id": conversation_id,
            "jira_key": jira_key,
            "intent_text": intent_text,
            "now": now,
        },
    )
    await record_event(
        session,
        task_id=task_id,
        actor="user",
        action="create",
        payload={
            "title": title,
            "intent_len": len(intent_text),
            "created_from": created_from,
            "jira_key": jira_key,
        },
    )
    return task_id
