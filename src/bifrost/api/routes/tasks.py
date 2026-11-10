"""Task REST API — Layer 1 surface.

Endpoints:
    POST /tasks                     Create from intent
    GET  /tasks                     List (filter by state)
    GET  /tasks/{id}                Detail with phases + events
    POST /tasks/{id}/advance        Approve current step, move to next
    POST /tasks/{id}/cancel         Hard cancel; worktree cleanup deferred
    POST /intent/classify            Standalone classifier (UI preview)

SSE stream + phase/plan endpoints land in later layers. This layer
gets just enough to create tasks and walk them through the state
machine manually.
"""

from __future__ import annotations

import asyncio
import logging
import uuid as _uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

logger = logging.getLogger(__name__)
from datetime import UTC

from pydantic import BaseModel
from sqlalchemy import text

from bifrost.actions import budget as _budget
from bifrost.actions import tools as _tools
from bifrost.actions.debugger import run_debug
from bifrost.actions.discussion_agent import run_discussion
from bifrost.actions.executor import execute_plan
from bifrost.actions.gap_detector import assess_coverage
from bifrost.actions.intent_router import classify_intent
from bifrost.actions.jira_enrich import enrich_task_from_jira
from bifrost.actions.orchestrator import execute_task
from bifrost.actions.plan_agent import (
    approve_context_artifact,
    approve_plan_artifact,
    run_plan,
)
from bifrost.actions.pr import PRError, create_pr
from bifrost.actions.quick import run_quick
from bifrost.actions.task_lifecycle import (
    IllegalTransition,
    TaskState,
    create_task,
    record_event,
    transition,
)
from bifrost.actions.verifier import override_verify_item, run_verification
from bifrost.actions.worktree import Worktree, WorktreeError
from bifrost.actions.worktree import remove as remove_worktree
from bifrost.api.auth.middleware import current_principal
from bifrost.api.auth.principal import ROLE_ADMIN, Principal
from bifrost.api.auth.roles import RoleRequired
from bifrost.api.deps import DB, AdminOnly, Settings, get_session_factory
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.cli.config import get_settings
from bifrost.extraction.cli_client import ClaudeCliError

router = APIRouter(tags=["tasks"])


# ── Schemas ──────────────────────────────────────────────────────


class IntentClassifyRequest(BaseModel):
    message: str


class IntentClassifyResponse(BaseModel):
    intent: str
    confidence: float
    reasoning: str
    jira_key: str | None = None
    clarifying_question: str | None = None


class CreateTaskRequest(BaseModel):
    title: str
    intent_text: str
    created_from: str = "direct"  # chat | jira | direct
    conversation_id: str | None = None
    jira_key: str | None = None


class TaskSummary(BaseModel):
    id: str
    title: str
    state: str
    created_from: str
    jira_key: str | None
    jira_url: str | None = None
    clarifications_pending: int
    phases_total: int
    phases_done: int
    # Rolled-up metrics for list view — cheap enough to compute on each
    # list query (bounded by task row count).
    total_cost_usd: float = 0.0
    ask_back_count: int = 0
    plans_total: int = 0
    plans_completed: int = 0
    plans_failed: int = 0
    has_worktree: bool = False
    has_pr: bool = False
    pr_url: str | None = None
    pr_number: int | None = None
    archived_at: str | None = None
    created_at: str
    updated_at: str


class TaskDetail(TaskSummary):
    intent_text: str
    conversation_id: str | None
    worktree_path: str | None
    branch_name: str | None
    pr_url: str | None = None
    pr_number: int | None = None
    pushed_at: str | None = None
    jira_enrichment: dict[str, Any] | None = None
    enriched_at: str | None = None
    acceptance_criteria: list[Any]
    events: list[dict[str, Any]]  # last 50


class AdvanceRequest(BaseModel):
    to_state: str
    reason: str = ""


# ── Routes ───────────────────────────────────────────────────────


@router.post(
    "/intent/classify",
    response_model=BifrostEnvelope[IntentClassifyResponse],
)
async def classify_intent_endpoint(
    body: IntentClassifyRequest,
) -> BifrostEnvelope[IntentClassifyResponse]:
    """Standalone intent classifier. UI uses this to show a preview
    before the user commits to creating a task."""
    result = await classify_intent(body.message)
    return BifrostEnvelope(data=IntentClassifyResponse(**result.as_dict()))


@router.post(
    "/tasks",
    response_model=BifrostEnvelope[TaskSummary],
    status_code=status.HTTP_201_CREATED,
)
async def create_task_endpoint(
    body: CreateTaskRequest,
    db: DB,
) -> BifrostEnvelope[TaskSummary]:
    if body.created_from not in ("chat", "jira", "direct"):
        raise HTTPException(400, "invalid created_from")
    task_id = await create_task(
        db,
        title=body.title,
        intent_text=body.intent_text,
        created_from=body.created_from,
        conversation_id=body.conversation_id,
        jira_key=body.jira_key,
    )
    await db.flush()
    # Inline enrichment when we have a Jira key — cheap cache lookup
    # when the issue is already indexed. Don't fail the request on
    # missing credentials or transient Jira errors.
    if body.jira_key:
        try:
            await enrich_task_from_jira(db, task_id=task_id)
        except Exception:
            logger.exception(
                "jira enrichment failed on task create (non-fatal)",
            )
    summary = await _load_summary(db, task_id)
    if summary is None:
        raise HTTPException(500, "task created but could not be loaded")
    return BifrostEnvelope(data=summary)


class TaskStats(BaseModel):
    active: int  # non-terminal tasks
    success: int  # PR open, awaiting review
    blocked: int
    cancelled: int
    pr_merged: int = 0  # PR merged → terminal SDLC done
    total_recent: int  # created in window
    cost_recent_usd: float  # total spend in window
    success_rate: float  # 0..1 over terminal tasks in window
    avg_cost_per_task: float
    window_days: int


@router.get(
    "/tasks/stats",
    response_model=BifrostEnvelope[TaskStats],
)
async def task_stats(
    db: DB,
    window_days: int = Query(default=7, ge=1, le=180),
) -> BifrostEnvelope[TaskStats]:
    """Header-card metrics for the tasks list page."""
    row = (
        await db.execute(
            text(
                """
                WITH recent AS (
                    SELECT b.id, b.state
                    FROM tasks b
                    WHERE b.created_at >= NOW() - make_interval(days => :w)
                ),
                cost AS (
                    SELECT COALESCE(SUM(a.research_cost), 0)::float AS c
                    FROM ask_backs a
                    JOIN tasks b ON b.id = a.task_id
                    WHERE b.created_at >= NOW() - make_interval(days => :w)
                )
                SELECT
                    (SELECT COUNT(*) FROM tasks
                     WHERE state NOT IN ('success','cancelled','blocked','pr_merged')) AS active,
                    (SELECT COUNT(*) FROM tasks WHERE state = 'success') AS success,
                    (SELECT COUNT(*) FROM tasks WHERE state = 'blocked') AS blocked,
                    (SELECT COUNT(*) FROM tasks WHERE state = 'cancelled') AS cancelled,
                    (SELECT COUNT(*) FROM recent) AS recent_total,
                    (SELECT c FROM cost) AS recent_cost,
                    (SELECT COUNT(*) FROM recent WHERE state IN ('success','pr_merged')) AS recent_success,
                    (SELECT COUNT(*) FROM recent
                     WHERE state IN ('success','cancelled','blocked','pr_merged')) AS recent_terminal,
                    (SELECT COUNT(*) FROM tasks WHERE state = 'pr_merged') AS pr_merged
                """
            ),
            {"w": window_days},
        )
    ).first()
    recent_total = int(row[4] or 0) if row else 0
    recent_cost = float(row[5] or 0) if row else 0.0
    recent_terminal = int(row[7] or 0) if row else 0
    recent_success = int(row[6] or 0) if row else 0
    return BifrostEnvelope(
        data=TaskStats(
            active=int(row[0] or 0) if row else 0,
            success=int(row[1] or 0) if row else 0,
            blocked=int(row[2] or 0) if row else 0,
            cancelled=int(row[3] or 0) if row else 0,
            pr_merged=int(row[8] or 0) if row else 0,
            total_recent=recent_total,
            cost_recent_usd=recent_cost,
            success_rate=((recent_success / recent_terminal) if recent_terminal else 0.0),
            avg_cost_per_task=((recent_cost / recent_total) if recent_total else 0.0),
            window_days=window_days,
        )
    )


@router.get(
    "/tasks",
    response_model=BifrostEnvelope[list[TaskSummary]],
)
async def list_tasks(
    db: DB,
    state: str | None = Query(None, description="Filter by state"),
    limit: int = Query(50, ge=1, le=500),
    include_archived: bool = Query(
        False,
        description="Include archived tasks in the result (default: hide)",
    ),
    archived_only: bool = Query(
        False,
        description="Return only archived tasks",
    ),
    q: str | None = Query(
        None,
        min_length=1,
        max_length=200,
        description=(
            "Case-insensitive substring match against title, intent_text, "
            "and jira_key. Whitespace-split terms are ANDed."
        ),
    ),
) -> BifrostEnvelope[list[TaskSummary]]:
    sql = "SELECT id FROM tasks"
    params: dict[str, Any] = {}
    where: list[str] = []
    if state:
        # Support semantic filters in addition to exact-match state.
        # ``open`` = anything still in flight (most common UI default).
        # ``closed`` = terminal states.
        if state == "open":
            where.append(
                "state NOT IN ('success', 'blocked', 'cancelled', 'pr_merged')",
            )
        elif state == "closed":
            # Includes pr_merged so the closed bucket actually contains
            # the SDLC-done tasks, not just PR-open-awaiting-review.
            where.append(
                "state IN ('success', 'blocked', 'cancelled', 'pr_merged')",
            )
        elif state == "shipped":
            # New filter: only fully-merged tasks. Useful for "what
            # closed this week?" rollups.
            where.append("state = 'pr_merged'")
        else:
            where.append("state = :state")
            params["state"] = state
    if archived_only:
        where.append("archived_at IS NOT NULL")
    elif not include_archived:
        where.append("archived_at IS NULL")
    if q:
        # Split on whitespace and AND the terms — "retry orders"
        # matches only tasks mentioning BOTH. Strip empties.
        terms = [t for t in q.strip().split() if t][:6]
        for i, term in enumerate(terms):
            key = f"q{i}"
            params[key] = f"%{term}%"
            where.append(
                "("
                f"title ILIKE :{key} OR "
                f"intent_text ILIKE :{key} OR "
                f"COALESCE(jira_key, '') ILIKE :{key}"
                ")"
            )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC LIMIT :lim"
    params["lim"] = limit
    rows = (await db.execute(text(sql), params)).all()
    out: list[TaskSummary] = []
    for (bid,) in rows:
        s = await _load_summary(db, str(bid))
        if s is not None:
            out.append(s)
    return BifrostEnvelope(data=out)


@router.get(
    "/tasks/{task_id}",
    response_model=BifrostEnvelope[TaskDetail],
)
async def get_task(
    task_id: str,
    db: DB,
) -> BifrostEnvelope[TaskDetail]:
    try:
        _uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(400, "invalid task id")
    detail = await _load_detail(db, task_id)
    if detail is None:
        raise HTTPException(404, "task not found")
    return BifrostEnvelope(data=detail)


@router.post(
    "/tasks/{task_id}/advance",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[TaskSummary],
)
async def advance_task(
    task_id: str,
    body: AdvanceRequest,
    db: DB,
) -> BifrostEnvelope[TaskSummary]:
    try:
        target = TaskState(body.to_state)
    except ValueError:
        raise HTTPException(400, f"unknown state: {body.to_state}")
    try:
        await transition(
            db,
            task_id=task_id,
            to_state=target,
            actor="user",
            reason=body.reason,
        )
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    await db.flush()
    summary = await _load_summary(db, task_id)
    if summary is None:
        raise HTTPException(500, "advance ok but reload failed")
    return BifrostEnvelope(data=summary)


class LinkRequest(BaseModel):
    conversation_id: str
    link_type: str = "reference"  # spawned_task | spawned_chat | reference | correction
    link_message_id: str | None = None
    note: str | None = None


class ChatSummary(BaseModel):
    conversation_id: str
    title: str
    link_type: str
    link_message_id: str | None
    note: str | None
    last_message_preview: str | None
    message_count: int
    linked_at: str


class SpawnTaskRequest(BaseModel):
    """Create a task pre-seeded from a chat message.

    The spawn modal supplies user-edited
    title + project + acceptance criteria + issue type. When
    ``project_key`` is set, the lifecycle hook will create a Jira issue
    immediately on PLAN_READY (or explicitly via ``create_jira_now``).
    """

    conversation_id: str
    message_id: str | None = None  # if set, use this message as the intent
    title: str | None = None  # override auto-extracted title
    override_intent: str | None = None  # override auto-extracted intent text
    # New (Phase A): operator-edited fields from the spawn modal
    project_key: str | None = None  # Jira project chosen by operator
    issue_type: str = "Task"  # Task | Story | Bug
    acceptance_criteria: list[str] | None = None
    create_jira_now: bool = False  # if true, create the Jira issue
    # immediately (skip waiting for
    # PLAN_READY); used by the spawn
    # modal "Create + go" button


class SpawnPreviewRequest(BaseModel):
    """Inputs for the preview endpoint that powers the spawn modal."""

    conversation_id: str
    message_id: str | None = None


class SpawnPreviewResponse(BaseModel):
    """Suggested defaults for the spawn modal — title, AC, project,
    type. The caller can edit any of these before sending POST
    /spawn-task. Suggested fields are best-guess only; the operator
    is the source of truth at create time."""

    suggested_title: str
    suggested_intent: str
    suggested_acceptance_criteria: list[str]
    suggested_issue_type: str
    resolved_project_key: str | None
    resolved_project_source: str
    resolved_project_reason: str
    candidate_projects: list[str]
    all_projects: list[str]  # full picker dropdown


class JiraRetractRequest(BaseModel):
    audit_id: str
    reason: str = ""


class FromJiraRequest(BaseModel):
    """Phase D — adopt an existing Jira ticket as a Bifrost task.

    The reverse direction of the spawn flow: instead of chat→task→
    Jira, the operator says "I have ticket SHIP-1234, work on it
    through Bifrost". The ticket's summary becomes the task title,
    its description (or AC parser output) becomes the intent +
    acceptance criteria, and its workflow state maps to a Bifrost
    state via a coarse mapping (see ``map_jira_state_to_task``).
    """

    jira_key: str
    conversation_id: str | None = None
    link_message_id: str | None = None


class PromoteToTaskRequest(BaseModel):
    """Push a chat message into a task as a correction clarification."""

    task_id: str
    message_id: str
    note: str | None = None


@router.post(
    "/tasks/{task_id}/link",
    response_model=BifrostEnvelope[dict[str, str]],
)
async def link_conversation(
    task_id: str,
    body: LinkRequest,
    db: DB,
) -> BifrostEnvelope[dict[str, str]]:
    """Attach a conversation to a task. Idempotent by (conv, task, type)."""
    if body.link_type not in ("spawned_task", "spawned_chat", "reference", "correction"):
        raise HTTPException(400, "invalid link_type")
    # Verify task + conversation exist
    exists = (
        await db.execute(
            text("SELECT 1 FROM tasks WHERE id = :id"),
            {"id": task_id},
        )
    ).first()
    if exists is None:
        raise HTTPException(404, "task not found")
    conv_exists = (
        await db.execute(
            text("SELECT 1 FROM conversations WHERE id = :id"),
            {"id": body.conversation_id},
        )
    ).first()
    if conv_exists is None:
        raise HTTPException(404, "conversation not found")
    # Upsert — re-linking with same type is a no-op; different type
    # creates a new row (e.g., "spawned_task" + later "correction").
    await db.execute(
        text(
            """
            INSERT INTO conversation_task_links
              (conversation_id, task_id, link_type, link_message_id, note, created_at, created_by)
            VALUES (:c, :b, :t, :m, :note, now(), 'user')
            ON CONFLICT (conversation_id, task_id, link_type) DO UPDATE
            SET link_message_id = EXCLUDED.link_message_id,
                note = EXCLUDED.note
            """
        ),
        {
            "c": body.conversation_id,
            "b": task_id,
            "t": body.link_type,
            "m": body.link_message_id,
            "note": body.note,
        },
    )
    await db.flush()
    return BifrostEnvelope(data={"status": "linked"})


@router.get(
    "/tasks/{task_id}/conversations",
    response_model=BifrostEnvelope[list[ChatSummary]],
)
async def task_conversations(
    task_id: str,
    db: DB,
) -> BifrostEnvelope[list[ChatSummary]]:
    """All chats that touch this task."""
    rows = (
        await db.execute(
            text(
                """
                SELECT l.conversation_id, c.title, l.link_type,
                       l.link_message_id, l.note, l.created_at,
                       (
                         SELECT content FROM conversation_messages
                         WHERE conversation_id = l.conversation_id
                         ORDER BY created_at DESC LIMIT 1
                       ) AS last_preview,
                       (
                         SELECT count(*) FROM conversation_messages
                         WHERE conversation_id = l.conversation_id
                       ) AS msg_count
                FROM conversation_task_links l
                JOIN conversations c ON c.id = l.conversation_id
                WHERE l.task_id = :bid
                ORDER BY l.created_at DESC
                """
            ),
            {"bid": task_id},
        )
    ).all()
    return BifrostEnvelope(
        data=[
            ChatSummary(
                conversation_id=str(r[0]),
                title=r[1] or "(untitled)",
                link_type=r[2],
                link_message_id=str(r[3]) if r[3] else None,
                note=r[4],
                last_message_preview=(r[6] or "")[:160] if r[6] else None,
                message_count=int(r[7] or 0),
                linked_at=r[5].isoformat() if r[5] else "",
            )
            for r in rows
        ]
    )


@router.get(
    "/conversations/{conversation_id}/tasks",
    response_model=BifrostEnvelope[list[dict[str, Any]]],
)
async def conversation_tasks(
    conversation_id: str,
    db: DB,
    settings: Settings,
) -> BifrostEnvelope[list[dict[str, Any]]]:
    """All tasks linked to this chat (reverse of task_conversations).

    Phase C of the SDLC redesign: includes ``jira_key`` + ``pr_url`` +
    ``pr_number`` so the chat lineage card can render Jira and PR
    pills inline at the message that spawned each task."""
    rows = (
        await db.execute(
            text(
                """
                SELECT l.task_id, b.title, b.state, l.link_type,
                       l.link_message_id, l.created_at,
                       b.jira_key, b.pr_url, b.pr_number
                FROM conversation_task_links l
                JOIN tasks b ON b.id = l.task_id
                WHERE l.conversation_id = :cid
                ORDER BY l.created_at DESC
                """
            ),
            {"cid": conversation_id},
        )
    ).all()
    return BifrostEnvelope(
        data=[
            {
                "task_id": str(r[0]),
                "title": r[1],
                "state": r[2],
                "link_type": r[3],
                "link_message_id": str(r[4]) if r[4] else None,
                "linked_at": r[5].isoformat() if r[5] else "",
                "jira_key": r[6],
                "jira_url": settings.sources.jira.browse_url(r[6]),
                "pr_url": r[7],
                "pr_number": r[8],
            }
            for r in rows
        ]
    )


@router.get(
    "/conversations/{conversation_id}/connections",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def conversation_connections(
    conversation_id: str,
    db: DB,
    settings: Settings,
) -> BifrostEnvelope[dict[str, Any]]:
    """Surface every object connected to this chat — tasks spawned,
    asks answered here, conversations covering similar scope.

    Powers the chat right-rail "Connections" panel and the small
    density badge on each thread row in the conversation list, so
    the operator never has to wonder "did this chat actually go
    anywhere?". Pure read aggregation, no LLM, no MCP.

    Sections:
      * tasks: spawned via ``conversation_task_links`` — the same
        join the inline lineage card already uses, so this stays
        consistent with what's rendered next to each user message.
      * asks: ``collab_asks`` rows whose ``answer_conversation_id``
        is this thread (the routed question landed here).
      * related: top conversations whose ``scope_repo_ids`` overlap
        this thread's scope, ordered by recent activity. Cheap proxy
        for "what other chats are touching the same repos?".
    """
    # ── Spawned tasks (via conversation_task_links) ───────────────
    task_rows = (
        await db.execute(
            text(
                """
                SELECT b.id::text, b.title, b.state, b.jira_key,
                       b.pr_url, b.pr_number, (b.pr_url IS NOT NULL) AS has_pr,
                       l.link_message_id::text, l.created_at,
                       b.updated_at,
                       b.clarifications_pending,
                       COALESCE((
                           SELECT COUNT(*) FROM task_plans p
                           JOIN task_phases ph ON ph.id = p.phase_id
                           WHERE ph.task_id = b.id
                       ), 0) AS plans_total,
                       COALESCE((
                           SELECT COUNT(*) FROM task_plans p
                           JOIN task_phases ph ON ph.id = p.phase_id
                           WHERE ph.task_id = b.id AND p.status = 'completed'
                       ), 0) AS plans_completed,
                       COALESCE((
                           SELECT COUNT(*) FROM task_plans p
                           JOIN task_phases ph ON ph.id = p.phase_id
                           WHERE ph.task_id = b.id AND p.status = 'failed'
                       ), 0) AS plans_failed,
                       COALESCE((
                           SELECT SUM(research_cost)::float FROM ask_backs a
                           WHERE a.task_id = b.id
                       ), 0) AS total_cost_usd
                FROM conversation_task_links l
                JOIN tasks b ON b.id = l.task_id
                WHERE l.conversation_id = :cid
                ORDER BY b.updated_at DESC
                LIMIT 25
                """
            ),
            {"cid": conversation_id},
        )
    ).all()

    # ── Asks answered here ────────────────────────────────────────
    ask_rows = (
        await db.execute(
            text(
                """
                SELECT id::text, question, asker_email, target_squad,
                       target_slack_channel, status, created_at,
                       answered_at, escalated_at
                FROM collab_asks
                WHERE answer_conversation_id = :cid
                ORDER BY COALESCE(answered_at, created_at) DESC
                LIMIT 15
                """
            ),
            {"cid": conversation_id},
        )
    ).all()

    # ── Related conversations (scope-overlap heuristic) ───────────
    # Cheap-and-good: grab THIS conversation's scope, then find others
    # whose ``scope_repo_ids`` intersects it (Postgres ``?|`` against a
    # text-array cast). Newest first; cap at 8.
    scope_row = (
        await db.execute(
            text(
                """
                SELECT scope_repo_ids
                FROM conversations
                WHERE id = :cid
                """
            ),
            {"cid": conversation_id},
        )
    ).first()
    related_rows: list[Any] = []
    if scope_row and scope_row[0]:
        repo_ids = scope_row[0]  # JSONB array of repo UUIDs
        if isinstance(repo_ids, list) and repo_ids:
            related_rows = (
                await db.execute(
                    text(
                        """
                        SELECT c.id::text, c.title, c.updated_at,
                               (SELECT COUNT(*) FROM conversation_messages cm
                                WHERE cm.conversation_id = c.id) AS message_count
                        FROM conversations c
                        WHERE c.id <> :cid
                          AND c.archived = FALSE
                          AND c.deleted_at IS NULL
                          AND c.scope_repo_ids ?| CAST(:repos AS text[])
                        ORDER BY c.updated_at DESC
                        LIMIT 8
                        """
                    ),
                    {
                        "cid": conversation_id,
                        "repos": [str(r) for r in repo_ids],
                    },
                )
            ).all()

    return BifrostEnvelope(
        data={
            "conversation_id": conversation_id,
            "tasks": [
                {
                    "task_id": r[0],
                    "title": r[1],
                    "state": r[2],
                    "jira_key": r[3],
                    "jira_url": settings.sources.jira.browse_url(r[3]),
                    "pr_url": r[4],
                    "pr_number": r[5],
                    "has_pr": bool(r[6]),
                    "link_message_id": r[7],
                    "linked_at": r[8].isoformat() if r[8] else "",
                    "updated_at": r[9].isoformat() if r[9] else "",
                    "clarifications_pending": int(r[10] or 0),
                    "plans_total": int(r[11] or 0),
                    "plans_completed": int(r[12] or 0),
                    "plans_failed": int(r[13] or 0),
                    "total_cost_usd": float(r[14] or 0),
                }
                for r in task_rows
            ],
            "asks": [
                {
                    "id": r[0],
                    "question": r[1],
                    "asker_email": r[2],
                    "target_squad": r[3],
                    "target_slack_channel": r[4],
                    "status": r[5],
                    "created_at": r[6].isoformat() if r[6] else "",
                    "answered_at": r[7].isoformat() if r[7] else None,
                    "escalated_at": r[8].isoformat() if r[8] else None,
                }
                for r in ask_rows
            ],
            "related": [
                {
                    "id": r[0],
                    "title": r[1],
                    "updated_at": r[2].isoformat() if r[2] else "",
                    "message_count": int(r[3] or 0),
                }
                for r in related_rows
            ],
            "counts": {
                "tasks": len(task_rows),
                "asks": len(ask_rows),
                "related": len(related_rows),
                "total": len(task_rows) + len(ask_rows),
            },
        }
    )


@router.post(
    "/conversations/{conversation_id}/spawn-task",
    response_model=BifrostEnvelope[TaskSummary],
    status_code=status.HTTP_201_CREATED,
)
async def spawn_task_from_chat(
    conversation_id: str,
    body: SpawnTaskRequest,
    db: DB,
    principal: Annotated[Principal, Depends(current_principal)],
) -> BifrostEnvelope[TaskSummary]:
    """Create a task pre-filled from a chat message's content.
    Automatically links the conversation with link_type='spawned_task'."""
    if body.create_jira_now and body.project_key and not principal.has_role(ROLE_ADMIN):
        raise RoleRequired("creating a Jira ticket requires the admin role")
    # Validate conversation exists
    conv_row = (
        await db.execute(
            text("SELECT title FROM conversations WHERE id = :id"),
            {"id": conversation_id},
        )
    ).first()
    if conv_row is None:
        raise HTTPException(404, "conversation not found")

    intent_text = body.override_intent
    if intent_text is None and body.message_id:
        msg_row = (
            await db.execute(
                text(
                    """
                    SELECT content FROM conversation_messages
                    WHERE id = :mid AND conversation_id = :cid
                    """
                ),
                {"mid": body.message_id, "cid": conversation_id},
            )
        ).first()
        if msg_row is None:
            raise HTTPException(404, "message not found in this conversation")
        intent_text = msg_row[0]

    if not intent_text:
        # Fall back to the conversation's last user message
        fallback = (
            await db.execute(
                text(
                    """
                    SELECT content FROM conversation_messages
                    WHERE conversation_id = :cid AND role = 'user'
                    ORDER BY created_at DESC LIMIT 1
                    """
                ),
                {"cid": conversation_id},
            )
        ).first()
        intent_text = fallback[0] if fallback else "(chat-spawned task)"

    title = body.title or (intent_text.split("\n")[0][:120]) or "Chat task"

    task_id = await create_task(
        db,
        title=title,
        intent_text=intent_text,
        created_from="chat",
        conversation_id=conversation_id,
    )
    # Persist user-edited acceptance criteria from the modal so the
    # subsequent Jira-create lifecycle hook surfaces them as the
    # ticket's Acceptance Criteria block.
    if body.acceptance_criteria:
        await db.execute(
            text(
                "UPDATE tasks SET acceptance_criteria = CAST(:ac AS JSONB), "
                "updated_at = NOW() WHERE id = CAST(:id AS uuid)"
            ),
            {
                "ac": _json_dumps(
                    [s for s in body.acceptance_criteria if s.strip()],
                ),
                "id": task_id,
            },
        )
    # Link
    await db.execute(
        text(
            """
            INSERT INTO conversation_task_links
              (conversation_id, task_id, link_type, link_message_id, note, created_at, created_by)
            VALUES (:c, :b, 'spawned_task', :m, :note, now(), 'user')
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "c": conversation_id,
            "b": task_id,
            "m": body.message_id,
            "note": "Spawned from chat",
        },
    )

    # Phase A: when the modal supplied a project_key + create_jira_now,
    # create the Jira issue inline so the response carries the new
    # jira_key. This is the "Create + go" path — the operator has
    # already reviewed everything and explicitly authorised the write.
    # Failures here are NON-FATAL: the task is still created (local-
    # only); the operator can re-sync via the retract+respawn path.
    #
    # Wrap with a 30s timeout so a hanging Atlassian MCP doesn't keep
    # the spawn modal at "Creating…" indefinitely. The underlying
    # jira_writer has its own per-call
    # timeout; this is the route-layer cap on TOTAL time the operator
    # waits at the modal.
    if body.create_jira_now and body.project_key:
        # Commit the create + AC update before firing the Jira write
        # so the writer sees the latest state.
        await db.flush()
        try:
            from bifrost.actions.jira_sync import ensure_jira_for_task

            await asyncio.wait_for(
                ensure_jira_for_task(
                    db,
                    task_id=task_id,
                    project_key=body.project_key,
                    actor="user",
                ),
                timeout=30.0,
            )
        except TimeoutError:
            logger.warning(
                "spawn_task_from_chat: Jira create timed out >30s for "
                "task=%s; task created locally, ticket may still be "
                "in flight (operator can retry from task detail)",
                task_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "spawn_task_from_chat: ensure_jira_for_task failed "
                "for task=%s; continuing without Jira link",
                task_id,
            )

    await db.flush()
    summary = await _load_summary(db, task_id)
    if summary is None:
        raise HTTPException(500, "created but reload failed")
    return BifrostEnvelope(data=summary)


def _json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, default=str)


# ── Spawn preview — powers the modal's defaults ────────────────────


@router.post(
    "/conversations/{conversation_id}/spawn-task/preview",
    response_model=BifrostEnvelope[SpawnPreviewResponse],
)
async def spawn_task_preview(
    conversation_id: str,
    body: SpawnPreviewRequest,
    db: DB,
) -> BifrostEnvelope[SpawnPreviewResponse]:
    """Return suggested defaults for the spawn modal.

    Read-only: does NOT create a task or write to Jira. The modal
    invokes this on open, populates its fields, then the operator
    edits + submits via POST /spawn-task. This is the contract that
    keeps "Create + go" the one and only Jira-mutating button."""
    from bifrost.actions.project_resolver import (
        all_known_projects,
        resolve_for_conversation,
    )

    # 1) Source intent text (same logic as spawn_task_from_chat)
    intent_text: str | None = None
    if body.message_id:
        msg_row = (
            await db.execute(
                text(
                    "SELECT content FROM conversation_messages "
                    "WHERE id = :mid AND conversation_id = :cid"
                ),
                {"mid": body.message_id, "cid": conversation_id},
            )
        ).first()
        if msg_row:
            intent_text = msg_row[0]
    if not intent_text:
        fallback = (
            await db.execute(
                text(
                    "SELECT content FROM conversation_messages "
                    "WHERE conversation_id = :cid AND role = 'user' "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"cid": conversation_id},
            )
        ).first()
        intent_text = fallback[0] if fallback else "(chat-spawned task)"

    # 2) Resolve project from conversation scope
    resolved = await resolve_for_conversation(
        db,
        conversation_id=conversation_id,
    )
    all_projects = await all_known_projects(db)

    # 3) Title + AC suggestion. Cheap heuristic: first line of intent
    # is the title; AC list is generated by a one-shot Haiku call,
    # gracefully falling back to a generic 3-bullet list if Claude
    # CLI isn't available (test bench / disconnected dev).
    suggested_title = (intent_text.split("\n")[0] or "")[:120].strip() or "Chat task"
    suggested_ac: list[str] = []
    suggested_type = "Task"
    try:
        from bifrost.actions.tools import suggest_acceptance_criteria

        suggested_ac, suggested_type = await suggest_acceptance_criteria(
            intent_text,
        )
    except Exception:  # noqa: BLE001
        # Best-effort. Modal will let the operator type their own AC
        # from scratch; we don't want a Claude CLI hiccup to block
        # the spawn flow.
        suggested_ac = [
            "Implementation matches the user's intent in the chat",
            "Tests cover the core behavior change",
            "No regression in existing functionality",
        ]
        suggested_type = "Task"

    return BifrostEnvelope(
        data=SpawnPreviewResponse(
            suggested_title=suggested_title,
            suggested_intent=intent_text,
            suggested_acceptance_criteria=list(suggested_ac),
            suggested_issue_type=suggested_type,
            resolved_project_key=(
                resolved.project_key if resolved and resolved.project_key else None
            ),
            resolved_project_source=(resolved.source if resolved else "no_signal"),
            resolved_project_reason=(
                resolved.reason if resolved else "no scope_repo_ids on this conversation"
            ),
            candidate_projects=list(resolved.candidates) if resolved else [],
            all_projects=all_projects,
        ),
    )


# ── Jira retract — reverse a Jira write logged in audit ────────────


@router.post(
    "/tasks/from-jira",
    response_model=BifrostEnvelope[TaskSummary],
    status_code=status.HTTP_201_CREATED,
)
async def task_from_jira(
    body: FromJiraRequest,
    db: DB,
) -> BifrostEnvelope[TaskSummary]:
    """Adopt an existing Jira ticket as a Bifrost task. Phase D of
    the SDLC redesign — closes the loop in the reverse direction.

    Flow:
      1. Create a task with ``created_from='jira'`` + ``jira_key`` set
         (no Jira write — the ticket already exists).
      2. Run ``jira_enrich`` to pull summary/description/acceptance
         criteria from the existing Jira issue.
      3. Map the Jira workflow state to a Bifrost state and
         transition the task there with ``fire_jira_hook=False`` so
         we don't bounce-write back to Jira.
      4. If a conversation_id was supplied, register the
         conversation_task_links row.
    """
    jira_key = body.jira_key.strip().upper()
    if not jira_key:
        raise HTTPException(400, "jira_key is required")
    # Validate format: <PROJECT>-<NUM>
    parts = jira_key.split("-", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        raise HTTPException(
            400,
            f"jira_key {jira_key!r} not a valid Jira issue key",
        )

    # Don't double-adopt the same ticket.
    existing = (
        await db.execute(
            text("SELECT id::text FROM tasks WHERE jira_key = :k LIMIT 1"),
            {"k": jira_key},
        )
    ).first()
    if existing:
        existing_id = str(existing[0])
        summary = await _load_summary(db, existing_id)
        if summary is None:
            raise HTTPException(409, f"task already exists for {jira_key}")
        return BifrostEnvelope(data=summary)

    # Provisional title — will be replaced by enrichment.
    task_id = await create_task(
        db,
        title=f"[{jira_key}] (loading from Jira…)",
        intent_text=f"Adopted from Jira ticket {jira_key}",
        created_from="jira",
        conversation_id=body.conversation_id,
        jira_key=jira_key,
    )

    if body.conversation_id:
        await db.execute(
            text(
                """
                INSERT INTO conversation_task_links
                  (conversation_id, task_id, link_type,
                   link_message_id, note, created_at, created_by)
                VALUES (:c, :b, 'reference', :m,
                        'Imported from Jira', now(), 'user')
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "c": body.conversation_id,
                "b": task_id,
                "m": body.link_message_id,
            },
        )

    await db.flush()
    # Enrich title + acceptance criteria from Jira. Persist the
    # failure on a soft flag so the FE can render a "Re-sync from
    # Jira" affordance and the operator isn't stuck with the
    # placeholder title forever.
    enrichment_failed: str | None = None
    try:
        await enrich_task_from_jira(db, task_id=task_id)
    except Exception as exc:  # noqa: BLE001
        enrichment_failed = str(exc)[:500]
        logger.exception(
            "task_from_jira: enrichment failed for %s; task created without "
            "AC. Operator can re-trigger from task detail.",
            jira_key,
        )
    if enrichment_failed:
        # Stamp a record event so the task detail page can render a
        # banner: "Jira enrichment failed: <msg>. [Re-sync]". The
        # task itself stays usable; AC + title remain the placeholder
        # values until a successful re-sync overwrites them.
        try:
            await record_event(
                db,
                task_id=task_id,
                actor="system",
                action="jira_enrich_failed",
                payload={"jira_key": jira_key, "error": enrichment_failed},
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "task_from_jira: record_event for enrich failure also "
                "failed; non-fatal but the FE banner won't render",
            )

    summary = await _load_summary(db, task_id)
    if summary is None:
        raise HTTPException(500, "created but reload failed")
    return BifrostEnvelope(data=summary)


@router.get(
    "/tasks/{task_id}/jira-audit",
    response_model=BifrostEnvelope[list[dict[str, Any]]],
)
async def jira_audit_list(
    task_id: str,
    db: DB,
    settings: Settings,
    limit: int = Query(50, ge=1, le=500),
) -> BifrostEnvelope[list[dict[str, Any]]]:
    """Return the Jira-write audit rows for a task, newest first.

    Powers the task-detail "Jira audit (N)" panel + per-row retract
    button. Pure read; no side effects."""
    rows = (
        (
            await db.execute(
                text(
                    """
                SELECT id::text AS id,
                       action,
                       jira_key,
                       before_state,
                       after_state,
                       jira_state_before,
                       jira_state_after,
                       ok,
                       error_message,
                       actor,
                       created_at,
                       retracted_at,
                       payload
                FROM task_jira_audit
                WHERE task_id = CAST(:tid AS uuid)
                ORDER BY created_at DESC
                LIMIT :limit
                """
                ),
                {"tid": task_id, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return BifrostEnvelope(
        data=[
            {
                "id": r["id"],
                "action": r["action"],
                "jira_key": r["jira_key"],
                "jira_url": settings.sources.jira.browse_url(r["jira_key"]),
                "before_state": r["before_state"],
                "after_state": r["after_state"],
                "jira_state_before": r["jira_state_before"],
                "jira_state_after": r["jira_state_after"],
                "ok": r["ok"],
                "error_message": r["error_message"],
                "actor": r["actor"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else "",
                "retracted_at": (r["retracted_at"].isoformat() if r["retracted_at"] else None),
                "payload": r["payload"] or {},
            }
            for r in rows
        ],
    )


@router.post(
    "/tasks/{task_id}/jira-retract",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def jira_retract(
    task_id: str,
    body: JiraRetractRequest,
    db: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    """Reverse a Jira write. Posts a [bifrost-retracted] comment and
    marks the audit row. For 'create' actions, also unlinks the task's
    jira_key so the operator can re-spawn into the right project."""
    from bifrost.actions.jira_writer import retract_audit

    try:
        out = await retract_audit(
            db,
            audit_id=body.audit_id,
            reason=body.reason or "(no reason)",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return BifrostEnvelope(data=out)


@router.post(
    "/tasks/{task_id}/promote-message",
    response_model=BifrostEnvelope[dict[str, str]],
)
async def promote_chat_message_to_task(
    task_id: str,
    body: PromoteToTaskRequest,
    db: DB,
) -> BifrostEnvelope[dict[str, str]]:
    """Turn a chat assistant reply into a pending clarification on a task.
    The Layer 6 cascade engine will pick this up and propose corrections
    against the task's belief DAG."""
    import uuid as _u

    if body.task_id != task_id:
        raise HTTPException(400, "task_id mismatch")
    msg_row = (
        await db.execute(
            text(
                """
                SELECT content, conversation_id
                FROM conversation_messages WHERE id = :mid
                """
            ),
            {"mid": body.message_id},
        )
    ).first()
    if msg_row is None:
        raise HTTPException(404, "message not found")
    content, conv_id = msg_row
    # Create a clarification of kind='conflict' so the task surfaces
    # it as "chat says X — agree / reject / regenerate plan?"
    clar_id = str(_u.uuid4())
    await db.execute(
        text(
            """
            INSERT INTO task_clarifications
              (id, task_id, source, kind, question, options, state, created_at)
            VALUES (:id, :bid, 'user', 'conflict', :q, CAST(:opts AS JSONB),
                    'open', now())
            """
        ),
        {
            "id": clar_id,
            "bid": task_id,
            "q": (
                "Chat reply promoted as potential correction:\n\n"
                + (content[:1500] if content else "")
                + (body.note or "")
            )[:4000],
            "opts": '["apply as correction","ignore","regenerate plan"]',
        },
    )
    # Register the chat-task link with type=correction
    await db.execute(
        text(
            """
            INSERT INTO conversation_task_links
              (conversation_id, task_id, link_type, link_message_id, note, created_at, created_by)
            VALUES (:c, :b, 'correction', :m, :note, now(), 'user')
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "c": str(conv_id),
            "b": task_id,
            "m": body.message_id,
            "note": body.note or "Promoted chat reply → task clarification",
        },
    )
    # Bump clarifications_pending
    await db.execute(
        text("UPDATE tasks SET clarifications_pending = clarifications_pending + 1 WHERE id = :id"),
        {"id": task_id},
    )
    await db.flush()
    return BifrostEnvelope(data={"clarification_id": clar_id})


@router.post(
    "/tasks/{task_id}/cancel",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[TaskSummary],
)
async def cancel_task(
    task_id: str,
    db: DB,
) -> BifrostEnvelope[TaskSummary]:
    try:
        await transition(
            db,
            task_id=task_id,
            to_state=TaskState.CANCELLED,
            actor="user",
            reason="user_cancel",
        )
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    await db.flush()
    summary = await _load_summary(db, task_id)
    if summary is None:
        raise HTTPException(500, "cancel ok but reload failed")
    return BifrostEnvelope(data=summary)


# ── Internal loaders ─────────────────────────────────────────────


async def _load_summary(session, task_id: str) -> TaskSummary | None:
    row = (
        await session.execute(
            text(
                """
                SELECT
                    b.id, b.title, b.state, b.created_from, b.jira_key,
                    b.clarifications_pending, b.phases_total, b.phases_done,
                    b.created_at, b.updated_at,
                    b.worktree_path, b.pr_url, b.pr_number, b.archived_at,
                    COALESCE((
                        SELECT SUM(research_cost)::float FROM ask_backs a
                        WHERE a.task_id = b.id
                    ), 0) AS total_cost,
                    COALESCE((
                        SELECT COUNT(*) FROM ask_backs a
                        WHERE a.task_id = b.id
                    ), 0) AS ask_back_count,
                    COALESCE((
                        SELECT COUNT(*) FROM task_plans p
                        JOIN task_phases ph ON ph.id = p.phase_id
                        WHERE ph.task_id = b.id
                    ), 0) AS plans_total,
                    COALESCE((
                        SELECT COUNT(*) FROM task_plans p
                        JOIN task_phases ph ON ph.id = p.phase_id
                        WHERE ph.task_id = b.id AND p.status = 'completed'
                    ), 0) AS plans_completed,
                    COALESCE((
                        SELECT COUNT(*) FROM task_plans p
                        JOIN task_phases ph ON ph.id = p.phase_id
                        WHERE ph.task_id = b.id AND p.status = 'failed'
                    ), 0) AS plans_failed
                FROM tasks b
                WHERE b.id = :id
                """
            ),
            {"id": task_id},
        )
    ).first()
    if row is None:
        return None
    return TaskSummary(
        id=str(row[0]),
        title=row[1],
        state=row[2],
        created_from=row[3],
        jira_key=row[4],
        jira_url=get_settings().sources.jira.browse_url(row[4]),
        clarifications_pending=int(row[5]),
        phases_total=int(row[6]),
        phases_done=int(row[7]),
        created_at=row[8].isoformat(),
        updated_at=row[9].isoformat(),
        has_worktree=row[10] is not None,
        pr_url=row[11],
        pr_number=int(row[12]) if row[12] is not None else None,
        has_pr=row[11] is not None,
        archived_at=row[13].isoformat() if row[13] is not None else None,
        total_cost_usd=float(row[14] or 0),
        ask_back_count=int(row[15] or 0),
        plans_total=int(row[16] or 0),
        plans_completed=int(row[17] or 0),
        plans_failed=int(row[18] or 0),
    )


async def _load_detail(session, task_id: str) -> TaskDetail | None:
    summary = await _load_summary(session, task_id)
    if summary is None:
        return None
    # Load remaining fields
    full = (
        await session.execute(
            text(
                """
                SELECT intent_text, conversation_id, worktree_path,
                       branch_name, acceptance_criteria, pr_url,
                       pr_number, pushed_at, jira_enrichment, enriched_at
                FROM tasks WHERE id = :id
                """
            ),
            {"id": task_id},
        )
    ).first()
    events = (
        await session.execute(
            text(
                """
                SELECT actor, action, payload, at
                FROM task_events
                WHERE task_id = :id
                ORDER BY at DESC LIMIT 50
                """
            ),
            {"id": task_id},
        )
    ).all()
    return TaskDetail(
        **summary.model_dump(),
        intent_text=full[0] if full else "",
        conversation_id=str(full[1]) if full and full[1] else None,
        worktree_path=full[2] if full else None,
        branch_name=full[3] if full else None,
        acceptance_criteria=list(full[4]) if full and full[4] else [],
        # pr_url / pr_number come from summary.model_dump() above (TaskSummary
        # already carries them) — passing them again here caused a duplicate-
        # keyword TypeError that 500'd every task-detail load.
        pushed_at=full[7].isoformat() if full and full[7] else None,
        jira_enrichment=full[8] if full else None,
        enriched_at=full[9].isoformat() if full and full[9] else None,
        events=[
            {
                "actor": e[0],
                "action": e[1],
                "payload": e[2],
                "at": e[3].isoformat(),
            }
            for e in events
        ],
    )


# ─────────────────────────────────────────────────────────────────────
# Layer 2 — Ask-back tools (manual smoke surface)
# ─────────────────────────────────────────────────────────────────────
#
# These endpoints are thin wrappers around ``bifrost.actions.tools``.
# They exist so a human (or a test) can poke the ask-back pipeline
# before we wire it into the real agent loop in Layer 3+. Each POST
# body maps 1:1 onto a tool function.


class ResearchToolRequest(BaseModel):
    question: str
    scope_repo_ids: list[str] | None = None
    phase_id: str | None = None
    agent_kind: str = "smoke"
    conversation_id: str | None = None
    gap_aware: bool = True
    use_full_pipeline: bool = False  # opt-in; defaults to graph-only


class ExtractToolRequest(BaseModel):
    repo_names: list[str]
    mode: str = "shallow"  # shallow | deep
    trigger_kind: str = "user_requested"
    phase_id: str | None = None
    clarification_id: str | None = None
    wait_seconds: int = 0
    max_files: int | None = None
    force_full: bool = False
    dry_run: bool = False


class AskUserToolRequest(BaseModel):
    question: str
    options: list[dict[str, Any]] | None = None
    kind: str = "need_user_input"
    blocks_state: str | None = None
    phase_id: str | None = None


class AnswerClarificationRequest(BaseModel):
    answer: str
    answered_by: str = "user"


class GapCheckRequest(BaseModel):
    question: str
    scope_repo_ids: list[str] | None = None


async def _assert_task_exists(session, task_id: str) -> None:
    exists = (
        await session.execute(
            text("SELECT 1 FROM tasks WHERE id = :id"),
            {"id": task_id},
        )
    ).first()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )


async def _assert_plan_belongs_to_task(session, task_id: str, plan_id: str) -> None:
    try:
        plan_uuid = _uuid.UUID(plan_id)
    except ValueError:
        plan_uuid = None
    owned = None
    if plan_uuid is not None:
        owned = (
            await session.execute(
                text(
                    "SELECT 1 FROM task_plans p JOIN task_phases ph ON ph.id = p.phase_id "
                    "WHERE p.id = :plan AND ph.task_id = :task"
                ),
                {"plan": plan_uuid, "task": task_id},
            )
        ).first()
    if owned is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plan {plan_id} not found on task {task_id}",
        )


# ── Budget snapshot ─────────────────────────────────────────────────


@router.get(
    "/tasks/{task_id}/budget",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def get_budget(
    task_id: str,
    session: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    await _assert_task_exists(session, task_id)
    snap = await _budget.snapshot(session, task_id)
    return BifrostEnvelope(data=snap.to_dict())


# ── Gap check (stateless; doesn't record an ask_back) ───────────────


@router.post(
    "/tasks/{task_id}/gap-check",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def gap_check(
    task_id: str,
    body: GapCheckRequest,
    session: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    await _assert_task_exists(session, task_id)
    gap = await assess_coverage(
        session,
        question=body.question,
        scope_repo_ids=body.scope_repo_ids,
    )
    return BifrostEnvelope(data=gap.to_dict())


# ── Research tool ────────────────────────────────────────────────────


@router.post(
    "/tasks/{task_id}/tools/research",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def tool_research(
    task_id: str,
    body: ResearchToolRequest,
    session: DB,
    settings: Settings,
) -> BifrostEnvelope[dict[str, Any]]:
    await _assert_task_exists(session, task_id)
    factory = None
    real_settings = None
    if body.use_full_pipeline:
        factory = get_session_factory(settings)
        real_settings = settings
    result = await _tools.bifrost_research(
        session,
        task_id=task_id,
        question=body.question,
        agent_kind=body.agent_kind,
        scope_repo_ids=body.scope_repo_ids,
        phase_id=body.phase_id,
        conversation_id=body.conversation_id,
        settings=real_settings,
        session_factory=factory,
        gap_aware=body.gap_aware,
        suggest_extractions=True,
    )
    return BifrostEnvelope(data=result.to_dict())


# ── Extraction tool ─────────────────────────────────────────────────


@router.post(
    "/tasks/{task_id}/tools/extract",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def tool_extract(
    task_id: str,
    body: ExtractToolRequest,
    session: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    await _assert_task_exists(session, task_id)
    result = await _tools.bifrost_trigger_extraction(
        session,
        task_id=task_id,
        scope_repo_names=body.repo_names,
        mode=body.mode,  # type: ignore[arg-type]
        trigger_kind=body.trigger_kind,  # type: ignore[arg-type]
        phase_id=body.phase_id,
        clarification_id=body.clarification_id,
        wait_seconds=body.wait_seconds,
        max_files=body.max_files,
        force_full=body.force_full,
        dry_run=body.dry_run,
    )
    return BifrostEnvelope(data=result.to_dict())


# ── Ask-user tool ────────────────────────────────────────────────────


@router.post(
    "/tasks/{task_id}/tools/ask-user",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def tool_ask_user(
    task_id: str,
    body: AskUserToolRequest,
    session: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    await _assert_task_exists(session, task_id)
    result = await _tools.bifrost_ask_user(
        session,
        task_id=task_id,
        question=body.question,
        options=body.options,
        kind=body.kind,  # type: ignore[arg-type]
        blocks_state=body.blocks_state,
        phase_id=body.phase_id,
    )
    return BifrostEnvelope(data=result.to_dict())


# ── Audit listing ────────────────────────────────────────────────────


@router.get(
    "/tasks/{task_id}/ask-backs",
    response_model=BifrostEnvelope[list[dict[str, Any]]],
)
async def list_ask_backs(
    task_id: str,
    session: DB,
    limit: int = Query(default=50, ge=1, le=500),
) -> BifrostEnvelope[list[dict[str, Any]]]:
    await _assert_task_exists(session, task_id)
    rows = (
        await session.execute(
            text(
                """
                SELECT id, phase_id, agent_kind, question, scope,
                       answered_from, triggered_extraction_id,
                       research_tokens, research_cost, duration_ms,
                       answer_summary, answer_confidence, created_at
                FROM ask_backs
                WHERE task_id = :id
                ORDER BY created_at DESC
                LIMIT :lim
                """
            ),
            {"id": task_id, "lim": limit},
        )
    ).all()
    return BifrostEnvelope(
        data=[
            {
                "id": str(r[0]),
                "phase_id": str(r[1]) if r[1] else None,
                "agent_kind": r[2],
                "question": r[3],
                "scope": r[4],
                "answered_from": r[5],
                "triggered_extraction_id": str(r[6]) if r[6] else None,
                "research_tokens": int(r[7] or 0),
                "research_cost": float(r[8] or 0),
                "duration_ms": int(r[9] or 0),
                "answer_summary": r[10],
                "answer_confidence": (float(r[11]) if r[11] is not None else None),
                "created_at": r[12].isoformat(),
            }
            for r in rows
        ]
    )


@router.get(
    "/tasks/{task_id}/clarifications",
    response_model=BifrostEnvelope[list[dict[str, Any]]],
)
async def list_clarifications(
    task_id: str,
    session: DB,
    state: str = Query(default="open"),
) -> BifrostEnvelope[list[dict[str, Any]]]:
    await _assert_task_exists(session, task_id)
    base = """
        SELECT id, phase_id, source, kind, question, options,
               blocks_state, state, answer, answered_by, answered_at,
               created_at
        FROM task_clarifications
        WHERE task_id = :id
    """
    params: dict[str, Any] = {"id": task_id}
    if state != "all":
        base += " AND state = :st"
        params["st"] = state
    base += " ORDER BY created_at DESC LIMIT 100"
    rows = (await session.execute(text(base), params)).all()
    return BifrostEnvelope(
        data=[
            {
                "id": str(r[0]),
                "phase_id": str(r[1]) if r[1] else None,
                "source": r[2],
                "kind": r[3],
                "question": r[4],
                "options": r[5],
                "blocks_state": r[6],
                "state": r[7],
                "answer": r[8],
                "answered_by": r[9],
                "answered_at": r[10].isoformat() if r[10] else None,
                "created_at": r[11].isoformat(),
            }
            for r in rows
        ]
    )


@router.post(
    "/tasks/{task_id}/clarifications/{clarification_id}/answer",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def answer_clarification_endpoint(
    task_id: str,
    clarification_id: str,
    body: AnswerClarificationRequest,
    session: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    await _assert_task_exists(session, task_id)
    try:
        result = await _tools.answer_clarification(
            session,
            clarification_id=clarification_id,
            answer=body.answer,
            answered_by=body.answered_by,  # type: ignore[arg-type]
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    return BifrostEnvelope(data=result)


# ── Extraction trigger audit ───────────────────────────────────────


@router.get(
    "/tasks/{task_id}/extractions",
    response_model=BifrostEnvelope[list[dict[str, Any]]],
)
async def list_extractions(
    task_id: str,
    session: DB,
    limit: int = Query(default=50, ge=1, le=200),
) -> BifrostEnvelope[list[dict[str, Any]]]:
    await _assert_task_exists(session, task_id)
    rows = (
        await session.execute(
            text(
                """
                SELECT et.id, et.extraction_task_id, et.trigger_kind,
                       et.phase_id, et.clarification_id, et.scope_repos,
                       et.mode, et.created_at, t.status
                FROM extraction_triggers et
                JOIN extraction_tasks t ON t.id = et.extraction_task_id
                WHERE et.task_id = :id
                ORDER BY et.created_at DESC
                LIMIT :lim
                """
            ),
            {"id": task_id, "lim": limit},
        )
    ).all()
    return BifrostEnvelope(
        data=[
            {
                "id": str(r[0]),
                "task_id": str(r[1]),
                "trigger_kind": r[2],
                "phase_id": str(r[3]) if r[3] else None,
                "clarification_id": str(r[4]) if r[4] else None,
                "scope_repos": r[5],
                "mode": r[6],
                "created_at": r[7].isoformat(),
                "task_status": r[8],
            }
            for r in rows
        ]
    )


# ─────────────────────────────────────────────────────────────────────
# Layer 3 — Discussion agent + phase/artifact introspection
# ─────────────────────────────────────────────────────────────────────


class DiscussRequest(BaseModel):
    user_message: str | None = None
    phase_id: str | None = None


@router.post(
    "/tasks/{task_id}/discuss",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def run_discussion_round(
    task_id: str,
    body: DiscussRequest,
    session: DB,
    settings: Settings,
) -> BifrostEnvelope[dict[str, Any]]:
    """Run one round of the discussion agent.

    Produces a new context artifact, writes typed assumptions,
    opens any blocking clarifications. Idempotent except for the
    version bump — calling it twice with no new input just yields
    version N+1 with the same content.
    """
    await _assert_task_exists(session, task_id)
    try:
        result = await run_discussion(
            session,
            task_id=task_id,
            user_message=body.user_message,
            phase_id=body.phase_id,
            settings=settings,
        )
    except ClaudeCliError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Discussion agent LLM call failed: {e}",
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    return BifrostEnvelope(data=result.to_dict())


@router.get(
    "/tasks/{task_id}/phases",
    response_model=BifrostEnvelope[list[dict[str, Any]]],
)
async def list_phases(
    task_id: str,
    session: DB,
) -> BifrostEnvelope[list[dict[str, Any]]]:
    await _assert_task_exists(session, task_id)
    rows = (
        await session.execute(
            text(
                """
                SELECT id, order_index, slug, title, state, current_step,
                       created_at, updated_at
                FROM task_phases
                WHERE task_id = :b
                ORDER BY order_index ASC
                """
            ),
            {"b": task_id},
        )
    ).all()
    return BifrostEnvelope(
        data=[
            {
                "id": str(r[0]),
                "order_index": int(r[1]),
                "slug": r[2],
                "title": r[3],
                "state": r[4],
                "current_step": r[5],
                "created_at": r[6].isoformat(),
                "updated_at": r[7].isoformat(),
            }
            for r in rows
        ]
    )


@router.get(
    "/tasks/{task_id}/artifacts",
    response_model=BifrostEnvelope[list[dict[str, Any]]],
)
async def list_artifacts(
    task_id: str,
    session: DB,
    kind: str | None = Query(default=None),
    phase_id: str | None = Query(default=None),
    latest_only: bool = Query(default=True),
) -> BifrostEnvelope[list[dict[str, Any]]]:
    """List artifacts for a task, optionally filtered by kind or phase.

    ``latest_only=true`` (default) returns only the highest-version row
    per (phase_id, kind). Set ``latest_only=false`` to get version
    history.
    """
    await _assert_task_exists(session, task_id)
    params: dict[str, Any] = {"b": task_id}
    where = ["bp.task_id = :b"]
    if kind:
        where.append("a.kind = :kind")
        params["kind"] = kind
    if phase_id:
        where.append("a.phase_id = :p")
        params["p"] = phase_id
    where_clause = " AND ".join(where)

    if latest_only:
        sql = f"""
            SELECT DISTINCT ON (a.phase_id, a.kind)
                a.id, a.phase_id, a.kind, a.version, a.state,
                a.body_md, a.parsed, a.produced_by_agent,
                a.created_at, a.approved_at, bp.title AS phase_title
            FROM task_artifacts a
            JOIN task_phases bp ON bp.id = a.phase_id
            WHERE {where_clause}
            ORDER BY a.phase_id, a.kind, a.version DESC
        """
    else:
        sql = f"""
            SELECT a.id, a.phase_id, a.kind, a.version, a.state,
                   a.body_md, a.parsed, a.produced_by_agent,
                   a.created_at, a.approved_at, bp.title AS phase_title
            FROM task_artifacts a
            JOIN task_phases bp ON bp.id = a.phase_id
            WHERE {where_clause}
            ORDER BY a.created_at DESC
        """
    rows = (await session.execute(text(sql), params)).all()
    return BifrostEnvelope(
        data=[
            {
                "id": str(r[0]),
                "phase_id": str(r[1]),
                "kind": r[2],
                "version": int(r[3]),
                "state": r[4],
                "body_md": r[5] or "",
                "parsed": r[6] or {},
                "produced_by_agent": r[7],
                "created_at": r[8].isoformat(),
                "approved_at": r[9].isoformat() if r[9] else None,
                "phase_title": r[10],
            }
            for r in rows
        ]
    )


@router.get(
    "/tasks/{task_id}/phases/{phase_id}/assumption-history",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def assumption_history(
    task_id: str,
    phase_id: str,
    session: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    """Per-key timeline of assumption values across artifact versions.

    Returns:
        {
            "phase_id": str,
            "versions": [{"artifact_id": str, "version": int,
                           "created_at": iso, "approved_at": iso|null,
                           "state": str}, ...],
            "keys": {
                "A1": [{"artifact_version": 1, "value": "...",
                        "tier": 1, "confidence": 1.0,
                        "source_type": "user_stated",
                        "state": "active",
                        "retracted_by_artifact_id": str|null}, ...],
                ...
            }
        }

    The ``keys`` map iterates in lexicographic A-key order.
    """
    await _assert_task_exists(session, task_id)
    # Verify the phase belongs to the task
    phase_row = (
        await session.execute(
            text(
                """
                SELECT id FROM task_phases
                WHERE id = :p AND task_id = :b
                """
            ),
            {"p": phase_id, "b": task_id},
        )
    ).first()
    if phase_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Phase {phase_id} not found on task {task_id}",
        )

    # Pull versions
    artifact_rows = (
        await session.execute(
            text(
                """
                SELECT id::text, kind, version, state, created_at, approved_at
                FROM task_artifacts
                WHERE phase_id = :p AND kind = 'context'
                ORDER BY version ASC
                """
            ),
            {"p": phase_id},
        )
    ).all()
    versions = [
        {
            "artifact_id": r[0],
            "kind": r[1],
            "version": int(r[2]),
            "state": r[3],
            "created_at": r[4].isoformat(),
            "approved_at": r[5].isoformat() if r[5] else None,
        }
        for r in artifact_rows
    ]

    # Pull every assumption on every version
    rows = (
        await session.execute(
            text(
                """
                SELECT
                    art.version AS artifact_version,
                    a.key,
                    a.value,
                    a.source_type,
                    a.confidence,
                    a.tier,
                    a.state,
                    a.retracted_by_artifact_id::text,
                    a.created_at
                FROM task_assumptions a
                JOIN task_artifacts art ON art.id = a.artifact_id
                WHERE art.phase_id = :p AND art.kind = 'context'
                ORDER BY a.key ASC, art.version ASC
                """
            ),
            {"p": phase_id},
        )
    ).all()

    keys: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        k = r[1]
        keys.setdefault(k, []).append(
            {
                "artifact_version": int(r[0]),
                "value": r[2],
                "source_type": r[3],
                "confidence": float(r[4]),
                "tier": int(r[5]),
                "state": r[6],
                "retracted_by_artifact_id": r[7],
                "created_at": r[8].isoformat(),
            }
        )

    return BifrostEnvelope(
        data={
            "phase_id": phase_id,
            "versions": versions,
            "keys": keys,
        }
    )


@router.get(
    "/tasks/{task_id}/artifacts/{artifact_id}/assumptions",
    response_model=BifrostEnvelope[list[dict[str, Any]]],
)
async def list_assumptions(
    task_id: str,
    artifact_id: str,
    session: DB,
    state: str = Query(default="active"),
) -> BifrostEnvelope[list[dict[str, Any]]]:
    """Atomic assumptions attached to one artifact. Default = active only."""
    await _assert_task_exists(session, task_id)
    where = ["a.artifact_id = :art"]
    params: dict[str, Any] = {"art": artifact_id}
    if state != "all":
        where.append("a.state = :st")
        params["st"] = state
    sql = (
        "SELECT a.id, a.key, a.value, a.source_type, a.confidence, "
        "a.tier, a.provenance, a.state, a.retracted_at, a.created_at "
        "FROM task_assumptions a WHERE " + " AND ".join(where) + " ORDER BY a.key ASC"
    )
    rows = (await session.execute(text(sql), params)).all()
    return BifrostEnvelope(
        data=[
            {
                "id": str(r[0]),
                "key": r[1],
                "value": r[2],
                "source_type": r[3],
                "confidence": float(r[4]),
                "tier": int(r[5]),
                "provenance": r[6] or {},
                "state": r[7],
                "retracted_at": r[8].isoformat() if r[8] else None,
                "created_at": r[9].isoformat(),
            }
            for r in rows
        ]
    )


# ─────────────────────────────────────────────────────────────────────
# Layer 5 — Plan agent + approval + correction cascade surfacing
# ─────────────────────────────────────────────────────────────────────


class PlanRequest(BaseModel):
    phase_id: str | None = None
    user_message: str | None = None


@router.post(
    "/tasks/{task_id}/plan",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def run_plan_round(
    task_id: str,
    body: PlanRequest,
    session: DB,
    settings: Settings,
) -> BifrostEnvelope[dict[str, Any]]:
    """Generate a plan artifact + task_plans rows for the approved context."""
    await _assert_task_exists(session, task_id)
    try:
        result = await run_plan(
            session,
            task_id=task_id,
            phase_id=body.phase_id,
            user_message=body.user_message,
            settings=settings,
        )
    except ClaudeCliError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Plan agent LLM call failed: {e}",
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return BifrostEnvelope(data=result.to_dict())


@router.get(
    "/tasks/{task_id}/plans",
    response_model=BifrostEnvelope[list[dict[str, Any]]],
)
async def list_plans(
    task_id: str,
    session: DB,
    phase_id: str | None = Query(default=None),
    artifact_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
) -> BifrostEnvelope[list[dict[str, Any]]]:
    await _assert_task_exists(session, task_id)
    where = ["bp.task_id = :b"]
    params: dict[str, Any] = {"b": task_id}
    if phase_id:
        where.append("p.phase_id = :ph")
        params["ph"] = phase_id
    if artifact_id:
        where.append("p.derived_from_artifact_id = :a")
        params["a"] = artifact_id
    if status_filter:
        where.append("p.status = :st")
        params["st"] = status_filter
    sql = f"""
        SELECT p.id, p.phase_id, p.derived_from_artifact_id, p.order_index,
               p.plan_key, p.name, p.files, p.urns, p.action_body,
               p.verify_cmd, p.done_criterion, p.confidence_basis, p.wave,
               p.depends_on, p.assumption_ids, p.status, p.git_sha,
               p.commit_msg, p.output_log, p.executed_at, p.created_at
        FROM task_plans p
        JOIN task_phases bp ON bp.id = p.phase_id
        WHERE {" AND ".join(where)}
        ORDER BY p.wave ASC, p.order_index ASC
    """
    rows = (await session.execute(text(sql), params)).all()
    return BifrostEnvelope(
        data=[
            {
                "id": str(r[0]),
                "phase_id": str(r[1]),
                "artifact_id": str(r[2]) if r[2] else None,
                "order_index": int(r[3]),
                "plan_key": r[4],
                "name": r[5],
                "files": r[6] or [],
                "urns": r[7] or [],
                "action_body": r[8],
                "verify_cmd": r[9],
                "done_criterion": r[10],
                "confidence_basis": r[11] or [],
                "wave": int(r[12]),
                "depends_on": r[13] or [],
                "assumption_ids": r[14] or [],
                "status": r[15],
                "git_sha": r[16],
                "commit_msg": r[17],
                "output_log": r[18],
                "executed_at": r[19].isoformat() if r[19] else None,
                "created_at": r[20].isoformat(),
            }
            for r in rows
        ]
    )


@router.get(
    "/tasks/{task_id}/verify-items",
    response_model=BifrostEnvelope[list[dict[str, Any]]],
)
async def list_verify_items(
    task_id: str,
    session: DB,
    phase_id: str | None = Query(default=None),
) -> BifrostEnvelope[list[dict[str, Any]]]:
    await _assert_task_exists(session, task_id)
    where = ["bp.task_id = :b"]
    params: dict[str, Any] = {"b": task_id}
    if phase_id:
        where.append("v.phase_id = :ph")
        params["ph"] = phase_id
    sql = f"""
        SELECT v.id, v.phase_id, v.plan_id, v.order_index, v.text,
               v.status, v.user_notes, v.answered_at
        FROM task_verify_items v
        JOIN task_phases bp ON bp.id = v.phase_id
        WHERE {" AND ".join(where)}
        ORDER BY v.order_index ASC
    """
    rows = (await session.execute(text(sql), params)).all()
    return BifrostEnvelope(
        data=[
            {
                "id": str(r[0]),
                "phase_id": str(r[1]),
                "plan_id": str(r[2]) if r[2] else None,
                "order_index": int(r[3]),
                "text": r[4],
                "status": r[5],
                "user_notes": r[6],
                "answered_at": r[7].isoformat() if r[7] else None,
            }
            for r in rows
        ]
    )


@router.post(
    "/tasks/{task_id}/artifacts/{artifact_id}/approve",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def approve_artifact(
    task_id: str,
    artifact_id: str,
    session: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    """Approve a context or plan artifact.

    - context approval: task DISCUSSING → DISCUSS_DONE
    - plan approval: task DRAFTING_PLAN → PLAN_READY; pending plans
      auto-flip to 'approved'.
    """
    await _assert_task_exists(session, task_id)
    row = (
        await session.execute(
            text("SELECT kind FROM task_artifacts WHERE id = :id"),
            {"id": artifact_id},
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"artifact {artifact_id} not found",
        )
    kind = row[0]
    try:
        if kind == "context":
            result = await approve_context_artifact(
                session,
                task_id=task_id,
                artifact_id=artifact_id,
            )
        elif kind == "plan":
            result = await approve_plan_artifact(
                session,
                task_id=task_id,
                artifact_id=artifact_id,
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"cannot approve artifact of kind '{kind}' yet",
            )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return BifrostEnvelope(data={"artifact_id": artifact_id, "kind": kind, **result})


# ─────────────────────────────────────────────────────────────────────
# Layer 6 — Worktree + executor
# ─────────────────────────────────────────────────────────────────────


class ExecuteRequest(BaseModel):
    target_repo_path: str
    # Reserved: dry_run (Layer 7), resume (default True)
    resume: bool = True


@router.post(
    "/tasks/{task_id}/execute",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def execute_task_endpoint(
    task_id: str,
    body: ExecuteRequest,
    session: DB,
    settings: Settings,
) -> BifrostEnvelope[dict[str, Any]]:
    """Run every approved plan in the task wave-by-wave.

    The caller supplies the absolute path of the git repo the executor
    should work inside. A worktree is created at
    ``<repo>/.claude/worktrees/task-<shortid>`` on branch
    ``bifrost/<shortid>-<slug>``.
    """
    await _assert_task_exists(session, task_id)
    try:
        result = await execute_task(
            session,
            task_id=task_id,
            target_repo_path=body.target_repo_path,
            settings=settings,
            resume=body.resume,
        )
    except WorktreeError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return BifrostEnvelope(data=result.to_dict())


class RetryPlanRequest(BaseModel):
    reset_status: bool = True  # flip failed → approved before re-run


@router.post(
    "/tasks/{task_id}/plans/{plan_id}/retry",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def retry_plan_endpoint(
    task_id: str,
    plan_id: str,
    body: RetryPlanRequest,
    session: DB,
    settings: Settings,
) -> BifrostEnvelope[dict[str, Any]]:
    """Re-run one plan in the existing worktree for a task."""
    await _assert_task_exists(session, task_id)
    # Need the task's worktree info
    row = (
        await session.execute(
            text("SELECT worktree_path, branch_name FROM tasks WHERE id = :id"),
            {"id": task_id},
        )
    ).first()
    if row is None or row[0] is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("Task has no worktree. Call /execute first to create one."),
        )
    wt = Worktree(
        task_id=task_id,
        repo_root="",  # unused by execute_plan
        path=row[0],
        branch=row[1] or "",
    )
    if body.reset_status:
        await session.execute(
            text(
                """
                UPDATE task_plans
                SET status = 'approved', output_log = NULL
                WHERE id = :id AND status IN ('failed','rejected')
                """
            ),
            {"id": plan_id},
        )
        await session.commit()
    try:
        outcome = await execute_plan(
            session,
            plan_id=plan_id,
            worktree=wt,
            settings=settings,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return BifrostEnvelope(data=outcome.to_dict())


@router.get(
    "/tasks/{task_id}/execution-status",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def execution_status(
    task_id: str,
    session: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    """Aggregate live view of executor progress."""
    await _assert_task_exists(session, task_id)
    task_row = (
        await session.execute(
            text(
                """
                SELECT state, worktree_path, branch_name
                FROM tasks WHERE id = :id
                """
            ),
            {"id": task_id},
        )
    ).first()
    plan_counts = (
        await session.execute(
            text(
                """
                SELECT p.status, COUNT(*)
                FROM task_plans p
                JOIN task_phases ph ON ph.id = p.phase_id
                WHERE ph.task_id = :b
                GROUP BY p.status
                """
            ),
            {"b": task_id},
        )
    ).all()
    counts = {s: int(c) for s, c in plan_counts}
    return BifrostEnvelope(
        data={
            "task_state": task_row[0],
            "worktree_path": task_row[1],
            "branch_name": task_row[2],
            "counts": {
                "pending": counts.get("pending", 0),
                "approved": counts.get("approved", 0),
                "executing": counts.get("executing", 0),
                "completed": counts.get("completed", 0),
                "failed": counts.get("failed", 0),
                "rejected": counts.get("rejected", 0),
            },
        }
    )


@router.post(
    "/tasks/{task_id}/worktree/remove",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def remove_worktree_endpoint(
    task_id: str,
    session: DB,
    force: bool = Query(default=False),
) -> BifrostEnvelope[dict[str, Any]]:
    """Delete the task's worktree + branch (doesn't touch commits)."""
    await _assert_task_exists(session, task_id)
    result = await remove_worktree(session, task_id=task_id, force=force)
    return BifrostEnvelope(data=result)


# ─────────────────────────────────────────────────────────────────────
# Layer 7 — Verifier agent + per-item override
# ─────────────────────────────────────────────────────────────────────


@router.post(
    "/tasks/{task_id}/verify",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def run_verification_endpoint(
    task_id: str,
    session: DB,
    settings: Settings,
) -> BifrostEnvelope[dict[str, Any]]:
    """Grade every pending acceptance check for the task."""
    await _assert_task_exists(session, task_id)
    try:
        result = await run_verification(
            session,
            task_id=task_id,
            settings=settings,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"verifier failed: {e}",
        ) from e
    return BifrostEnvelope(data=result.to_dict())


class VerifyOverrideRequest(BaseModel):
    verdict: str  # pass | fail | skipped
    notes: str | None = None


@router.post(
    "/tasks/{task_id}/verify-items/{item_id}/override",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def verify_item_override(
    task_id: str,
    item_id: str,
    body: VerifyOverrideRequest,
    session: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    """User forces a verify-item verdict; re-evaluates task lifecycle."""
    await _assert_task_exists(session, task_id)
    try:
        result = await override_verify_item(
            session,
            task_id=task_id,
            item_id=item_id,
            verdict=body.verdict,
            notes=body.notes,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return BifrostEnvelope(data=result)


# ─────────────────────────────────────────────────────────────────────
# Layer 8 — PR creation + debug helper
# ─────────────────────────────────────────────────────────────────────


class CreatePRRequest(BaseModel):
    base_branch: str = "main"
    draft: bool = False
    use_gh: bool = True
    dry_run: bool = False


@router.post(
    "/tasks/{task_id}/create-pr",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def create_pr_endpoint(
    task_id: str,
    body: CreatePRRequest,
    session: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    """Push the task's branch and (optionally) open a PR via ``gh``.

    Use ``dry_run=True`` to preview the title + body + commands without
    touching git or gh.
    """
    await _assert_task_exists(session, task_id)
    try:
        result = await create_pr(
            session,
            task_id=task_id,
            base_branch=body.base_branch,
            draft=body.draft,
            use_gh=body.use_gh,
            dry_run=body.dry_run,
        )
    except PRError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return BifrostEnvelope(data=result.to_dict())


@router.post(
    "/tasks/{task_id}/plans/{plan_id}/debug",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def debug_plan_endpoint(
    task_id: str,
    plan_id: str,
    session: DB,
    settings: Settings,
) -> BifrostEnvelope[dict[str, Any]]:
    """Diagnose a failed plan. Spawns a Bash-capable agent, so admin only."""
    await _assert_task_exists(session, task_id)
    await _assert_plan_belongs_to_task(session, task_id, plan_id)
    try:
        report = await run_debug(
            session,
            plan_id=plan_id,
            settings=settings,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return BifrostEnvelope(data=report.to_dict())


# ─────────────────────────────────────────────────────────────────────
# Layer 9 — /quick fast path
# ─────────────────────────────────────────────────────────────────────


class QuickRequest(BaseModel):
    message: str
    target_repo_path: str
    title: str | None = None
    jira_key: str | None = None
    skip_verify: bool = False


@router.post(
    "/quick",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def quick_endpoint(
    body: QuickRequest,
    session: DB,
    settings: Settings,
) -> BifrostEnvelope[dict[str, Any]]:
    """One-shot pipeline for trivial changes.

    Skips the multi-round discuss/plan/approve loop: Haiku drafts a
    single-task plan, which auto-advances through every lifecycle
    state up to PLAN_READY, then the existing orchestrator +
    verifier run inline.

    When the message is too ambiguous, returns ``fallback_to_full:
    true`` and the caller should create a regular task.
    """
    try:
        result = await run_quick(
            session,
            message=body.message,
            target_repo_path=body.target_repo_path,
            title=body.title,
            jira_key=body.jira_key,
            skip_verify=body.skip_verify,
            settings=settings,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return BifrostEnvelope(data=result.to_dict())


# ─────────────────────────────────────────────────────────────────────
# Layer 10 polish — manual Jira enrichment retry
# ─────────────────────────────────────────────────────────────────────


class EnrichFromJiraRequest(BaseModel):
    force_refresh: bool = False


@router.post(
    "/tasks/{task_id}/enrich-from-jira",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def enrich_from_jira_endpoint(
    task_id: str,
    body: EnrichFromJiraRequest,
    session: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    """Re-fetch Jira data and re-parse AC / Confluence links.

    ``force_refresh=true`` bypasses the "already enriched" short-
    circuit — useful when the ticket has changed since the task was
    created.
    """
    await _assert_task_exists(session, task_id)
    try:
        result = await enrich_task_from_jira(
            session,
            task_id=task_id,
            force_refresh=body.force_refresh,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    return BifrostEnvelope(data=result.to_dict())


# ─────────────────────────────────────────────────────────────────────
# Archive — soft-hide terminal tasks so the list stays triageable
# ─────────────────────────────────────────────────────────────────────


_TERMINAL_STATES_FOR_ARCHIVE = frozenset(
    {"success", "cancelled", "blocked"},
)


@router.post(
    "/tasks/{task_id}/archive",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def archive_task(
    task_id: str,
    session: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    """Flag a task as archived (soft-hide from the default list).

    Allowed at any point — user may want to hide an in-progress task
    too. Doesn't cancel or delete anything.
    """
    await _assert_task_exists(session, task_id)
    from datetime import datetime as _dt

    now = _dt.now(UTC)
    row = (
        await session.execute(
            text(
                """
                UPDATE tasks
                SET archived_at = :at, updated_at = NOW()
                WHERE id = :id AND archived_at IS NULL
                RETURNING id
                """
            ),
            {"at": now, "id": task_id},
        )
    ).first()
    changed = row is not None
    if changed:
        await record_event(
            session,
            task_id=task_id,
            actor="user",
            action="archived",
            payload={"archived_at": now.isoformat()},
        )
    return BifrostEnvelope(
        data={
            "task_id": task_id,
            "archived": True,
            "already_archived": not changed,
            "archived_at": now.isoformat() if changed else None,
        }
    )


@router.post(
    "/tasks/{task_id}/unarchive",
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def unarchive_task(
    task_id: str,
    session: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    """Restore a task to the live list."""
    await _assert_task_exists(session, task_id)
    row = (
        await session.execute(
            text(
                """
                UPDATE tasks
                SET archived_at = NULL, updated_at = NOW()
                WHERE id = :id AND archived_at IS NOT NULL
                RETURNING id
                """
            ),
            {"id": task_id},
        )
    ).first()
    changed = row is not None
    if changed:
        await record_event(
            session,
            task_id=task_id,
            actor="user",
            action="unarchived",
            payload={},
        )
    return BifrostEnvelope(
        data={
            "task_id": task_id,
            "archived": False,
            "was_archived": changed,
        }
    )


class AutoArchiveRequest(BaseModel):
    older_than_days: int = 14
    dry_run: bool = True


@router.post(
    "/tasks/auto-archive",
    dependencies=[AdminOnly],
    response_model=BifrostEnvelope[dict[str, Any]],
)
async def auto_archive(
    body: AutoArchiveRequest,
    session: DB,
) -> BifrostEnvelope[dict[str, Any]]:
    """Sweep terminal tasks (success/cancelled/blocked) older than N days.

    Wraps ``actions.auto_archive.sweep_terminal_tasks`` so manual and
    scheduled sweeps can't diverge. Returns the candidate list for UI
    preview when ``dry_run=True``.
    """
    # Preview: list the rows so the UI can show what's about to happen.
    candidate_rows = (
        await session.execute(
            text(
                """
                SELECT id::text, state, updated_at
                FROM tasks
                WHERE archived_at IS NULL
                  AND state = ANY(CAST(:states AS text[]))
                  AND updated_at < NOW() - make_interval(days => :days)
                ORDER BY updated_at ASC
                """
            ),
            {
                "states": list(_TERMINAL_STATES_FOR_ARCHIVE),
                "days": body.older_than_days,
            },
        )
    ).all()
    candidates = [
        {"id": r[0], "state": r[1], "updated_at": r[2].isoformat()} for r in candidate_rows
    ]
    if body.dry_run or not candidates:
        return BifrostEnvelope(
            data={
                "dry_run": body.dry_run,
                "candidates": candidates,
                "archived": 0,
            }
        )

    from bifrost.actions.auto_archive import sweep_terminal_tasks

    result = await sweep_terminal_tasks(
        session,
        older_than_days=body.older_than_days,
        dry_run=False,
    )
    return BifrostEnvelope(
        data={
            "dry_run": False,
            "candidates": candidates,
            "archived": len(result.archived_ids),
        }
    )
