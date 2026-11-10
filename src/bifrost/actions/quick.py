"""Quick-mode pipeline — one-shot task for trivial changes.

The full discuss → approve → plan → approve → execute → verify loop is
right for ambiguous work. For "rename this var", "fix this typo",
"add this one-liner" it's overkill. The quick path:

    1. Create the task (state=draft_intent).
    2. Ask Haiku for a *minimal* 1-task plan: files, action_body,
       verify_cmd, done_criterion, acceptance checks (1-3).
    3. Synthesize a context artifact + plan artifact directly from
       the message (no discussion round) and auto-approve them.
    4. Write one ``task_plans`` row + matching ``task_verify_items``.
    5. Run the existing orchestrator against the caller-supplied
       ``target_repo_path``.
    6. If executor completes, run the verifier.
    7. Return full summary.

Trade-offs
----------
* No multi-turn refinement. If the LLM misreads the intent, the user
  sees a failed plan and can either retry with hints or switch to
  full mode.
* Assumptions list is skipped — there's no DAG benefit for one-off
  edits.
* Budget is still enforced (same ``tasks.budget_*`` caps).

Typical cost: one Haiku plan-stub (~$0.001) + one Sonnet executor
(~$0.05) + one Sonnet verify (~$0.10) = ~$0.15 end-to-end. Wall time
~45-90 seconds depending on verify complexity.
"""

from __future__ import annotations

import json
import logging
import time
import uuid as _uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.actions._prompt_safety import BOUNDARY_SENTENCE, wrap_untrusted
from bifrost.actions.orchestrator import execute_task
from bifrost.actions.task_lifecycle import (
    TaskState,
    create_task,
    record_event,
    transition,
)
from bifrost.actions.verifier import run_verification
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient

logger = logging.getLogger(__name__)


# ── Schema for the quick plan Haiku call ────────────────────────────


_QUICK_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "title",
        "context_md",
        "plan_md",
        "task",
        "verify_items",
        "ready_to_execute",
    ],
    "properties": {
        "title": {
            "type": "string",
            "description": "≤60 char summary suitable for a task title.",
        },
        "context_md": {
            "type": "string",
            "description": (
                "4-8 lines: ## Goal + ## Constraints. Short because "
                "quick mode skips a discussion round."
            ),
        },
        "plan_md": {
            "type": "string",
            "description": ("3-6 lines. ## Overview + any one-sentence ## Risks."),
        },
        "task": {
            "type": "object",
            "required": ["name", "files", "action_body", "done_criterion"],
            "properties": {
                "name": {"type": "string"},
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "urns": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "action_body": {"type": "string"},
                "verify_cmd": {"type": "string"},
                "done_criterion": {"type": "string"},
            },
        },
        "verify_items": {
            "type": "array",
            "items": {"type": "string"},
            "description": "1-4 acceptance checks (plain language).",
        },
        "ready_to_execute": {
            "type": "boolean",
            "description": (
                "False only when the message is too vague to produce "
                "a concrete 1-task plan. Caller should then fall back "
                "to the full discuss/plan loop."
            ),
        },
    },
}


_QUICK_SYSTEM_PROMPT = """\
You are Bifrost's quick-mode planner. The user gave a short request
for a trivial code change — typo fix, rename, one-liner, tiny doc tweak.
Your job: turn it into EXACTLY ONE atomic task that the executor agent
will apply inside a git worktree.

HARD RULES:

1. ONE TASK. No waves. No depends_on. No sub-tasks. If the request
   genuinely needs more than one task, return ``ready_to_execute=false``
   and say so in ``plan_md`` — the caller will fall back to full mode.
2. FILES MUST BE SPECIFIC. Best-effort guess is fine if paths are
   unambiguous from the request; otherwise list the directory and let
   the executor agent discover the exact file. Never list more than 3.
3. ACTION_BODY IS CONCRETE. Imperative, one paragraph, ~3-5 sentences.
   Describe WHAT to change and WHERE, not step-by-step HOW — the
   executor agent already knows how to use Edit/Write/Bash.
4. VERIFY_CMD IS A SINGLE SHELL COMMAND OR EMPTY.
   Good: ``pytest tests/test_thing.py -q``, ``python -m py_compile app.py``,
   empty string (conversational verify only).
   Bad: multi-line, pipelines that write files, commands that install.
5. VERIFY_ITEMS ARE 1-4 plain-language acceptance checks. Don't pad.

__BIFROST_BOUNDARY_SENTINEL__

Respond with ONLY the JSON object the schema expects.
""".replace("__BIFROST_BOUNDARY_SENTINEL__", BOUNDARY_SENTENCE)


# ── Result type ─────────────────────────────────────────────────────


@dataclass
class QuickRunResult:
    task_id: str
    title: str
    final_state: str
    plan_key: str
    plan_status: str
    git_sha: str | None
    verify_passed: int
    verify_failed: int
    verify_skipped: int
    ready_for_pr: bool
    total_cost_usd: float
    duration_ms: int
    plan_md: str = ""
    context_md: str = ""
    fallback_to_full: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Entry point ─────────────────────────────────────────────────────


async def run_quick(
    session: AsyncSession,
    *,
    message: str,
    target_repo_path: str,
    title: str | None = None,
    jira_key: str | None = None,
    skip_verify: bool = False,
    settings: BifrostSettings | None = None,
) -> QuickRunResult:
    """Run the full quick-mode pipeline end-to-end."""
    started = time.monotonic()
    settings = settings or get_settings()

    # 1. LLM plan-stub (Haiku, 1-shot)
    stub_cost = 0.0
    client = ClaudeLlmClient(settings)
    try:
        # Wrap user message in <user_request> so the model treats
        # it as data, not an instruction stream.
        completion = await client.call_with_json_schema(
            system_prompt=_QUICK_SYSTEM_PROMPT,
            user_message=f"User request:\n\n{wrap_untrusted(message, 'user_request')}",
            model=settings.triage_model,  # Haiku
            json_schema=_QUICK_PLAN_SCHEMA,
            max_turns=1,
            timeout_override=60,
        )
    except ClaudeCliError as e:
        raise RuntimeError(f"quick-planner LLM failed: {e}") from e
    data = completion.parsed or {}
    usage = completion.usage
    stub_cost = float(getattr(usage, "total_cost_usd", 0.0) or 0.0)

    if not data.get("ready_to_execute", False):
        return QuickRunResult(
            task_id="",
            title=str(data.get("title") or message[:60]),
            final_state="fallback",
            plan_key="",
            plan_status="not_created",
            git_sha=None,
            verify_passed=0,
            verify_failed=0,
            verify_skipped=0,
            ready_for_pr=False,
            total_cost_usd=stub_cost,
            duration_ms=int((time.monotonic() - started) * 1000),
            plan_md=str(data.get("plan_md") or ""),
            context_md=str(data.get("context_md") or ""),
            fallback_to_full=True,
            notes=[
                "LLM returned ready_to_execute=false — run the full discuss/plan pipeline instead.",
            ],
        )

    task = data.get("task") or {}
    title_final = title or str(data.get("title") or "").strip() or message[:60]

    # 2. Create task
    task_id = await create_task(
        session,
        title=title_final,
        intent_text=message,
        created_from="direct",
        jira_key=jira_key,
    )
    await session.commit()

    # 3. Ensure phase + write auto-approved context + plan artifacts
    phase_id = await _ensure_main_phase(session, task_id)
    await _insert_artifact(
        session,
        phase_id=phase_id,
        kind="context",
        body_md=str(data.get("context_md") or message),
        parsed={"quick_mode": True, "ready_to_plan": True},
        produced_by="quick_agent",
        approve=True,
    )
    plan_artifact_id = await _insert_artifact(
        session,
        phase_id=phase_id,
        kind="plan",
        body_md=str(data.get("plan_md") or ""),
        parsed={
            "quick_mode": True,
            "ready_to_execute": True,
            "task_count": 1,
            "verify_item_count": len(data.get("verify_items") or []),
        },
        produced_by="quick_agent",
        approve=True,
    )

    # 4. Single plan row (status=approved so orchestrator picks it up)
    plan_id = await _insert_plan_row(
        session,
        phase_id=phase_id,
        artifact_id=plan_artifact_id,
        task=task,
    )

    # 5. Verify items
    for idx, v in enumerate(data.get("verify_items") or []):
        await session.execute(
            text(
                """
                INSERT INTO task_verify_items
                    (id, phase_id, plan_id, order_index, text, status)
                VALUES
                    (:id, :p, :pid, :idx, :t, 'pending')
                """
            ),
            {
                "id": str(_uuid.uuid4()),
                "p": phase_id,
                "pid": plan_id,
                "idx": idx,
                "t": str(v)[:4000],
            },
        )

    # 6. Lifecycle: DRAFT_INTENT → DISCUSSING → DISCUSS_DONE → DRAFTING_PLAN → PLAN_READY
    for to_state, reason in (
        (TaskState.DISCUSSING, "quick-mode auto-discuss"),
        (TaskState.DISCUSS_DONE, "quick-mode auto-approve context"),
        (TaskState.DRAFTING_PLAN, "quick-mode auto-draft-plan"),
        (TaskState.PLAN_READY, "quick-mode auto-approve plan"),
    ):
        try:
            await transition(
                session,
                task_id=task_id,
                to_state=to_state,
                actor="agent:quick",
                reason=reason,
            )
        except Exception:
            logger.exception(
                "quick: lifecycle transition to %s failed",
                to_state,
            )
    await record_event(
        session,
        task_id=task_id,
        actor="agent:quick",
        action="quick_prep",
        payload={
            "plan_id": plan_id,
            "verify_items": len(data.get("verify_items") or []),
            "cost_usd": stub_cost,
        },
    )
    await session.commit()

    # 7. Execute + verify using the existing orchestrator/verifier
    total_cost = stub_cost
    exec_result = await execute_task(
        session,
        task_id=task_id,
        target_repo_path=target_repo_path,
        settings=settings,
    )
    total_cost += exec_result.total_cost_usd

    verify_p = verify_f = verify_s = 0
    ready_for_pr = False
    final_state = exec_result.final_task_state
    if not skip_verify and final_state == TaskState.VERIFYING.value:
        try:
            vr = await run_verification(
                session,
                task_id=task_id,
                settings=settings,
            )
            verify_p = vr.passed
            verify_f = vr.failed
            verify_s = vr.skipped
            ready_for_pr = vr.ready_for_pr
            final_state = vr.final_task_state
            total_cost += vr.cost_usd
        except Exception:
            logger.exception("quick: verify step failed")

    # 8. Pull sha of the one plan
    plan_row = (
        await session.execute(
            text(
                """
                SELECT plan_key, status, git_sha FROM task_plans
                WHERE id = :id
                """
            ),
            {"id": plan_id},
        )
    ).first()

    return QuickRunResult(
        task_id=task_id,
        title=title_final,
        final_state=final_state,
        plan_key=plan_row[0] if plan_row else (task.get("name") or "T1"),
        plan_status=plan_row[1] if plan_row else "unknown",
        git_sha=plan_row[2] if plan_row else None,
        verify_passed=verify_p,
        verify_failed=verify_f,
        verify_skipped=verify_s,
        ready_for_pr=ready_for_pr,
        total_cost_usd=total_cost,
        duration_ms=int((time.monotonic() - started) * 1000),
        plan_md=str(data.get("plan_md") or ""),
        context_md=str(data.get("context_md") or ""),
        fallback_to_full=False,
    )


# ── Helpers ────────────────────────────────────────────────────────


async def _ensure_main_phase(session: AsyncSession, task_id: str) -> str:
    existing = (
        await session.execute(
            text(
                """
                SELECT id FROM task_phases
                WHERE task_id = :b
                ORDER BY order_index ASC
                LIMIT 1
                """
            ),
            {"b": task_id},
        )
    ).first()
    if existing is not None:
        return str(existing[0])
    phase_id = str(_uuid.uuid4())
    await session.execute(
        text(
            """
            INSERT INTO task_phases
                (id, task_id, order_index, slug, title, state, current_step)
            VALUES
                (:id, :task, 0, 'main', 'Main', 'drafting_discuss', 'discuss')
            """
        ),
        {"id": phase_id, "task": task_id},
    )
    await session.execute(
        text("UPDATE tasks SET phases_total = 1, updated_at = NOW() WHERE id = :id"),
        {"id": task_id},
    )
    return phase_id


async def _insert_artifact(
    session: AsyncSession,
    *,
    phase_id: str,
    kind: str,
    body_md: str,
    parsed: dict[str, Any],
    produced_by: str,
    approve: bool,
) -> str:
    aid = str(_uuid.uuid4())
    state = "approved" if approve else "draft"
    await session.execute(
        text(
            """
            INSERT INTO task_artifacts
                (id, phase_id, kind, version, body_md, state,
                 parsed, produced_by_agent,
                 approved_at)
            VALUES
                (:id, :p, :k, 1, :body, :state,
                 CAST(:parsed AS JSONB), :agent,
                 CASE WHEN :state = 'approved' THEN NOW() ELSE NULL END)
            """
        ),
        {
            "id": aid,
            "p": phase_id,
            "k": kind,
            "body": body_md,
            "parsed": json.dumps(parsed),
            "agent": produced_by,
            "state": state,
        },
    )
    return aid


async def _insert_plan_row(
    session: AsyncSession,
    *,
    phase_id: str,
    artifact_id: str,
    task: dict[str, Any],
) -> str:
    pid = str(_uuid.uuid4())
    await session.execute(
        text(
            """
            INSERT INTO task_plans
                (id, phase_id, derived_from_artifact_id, order_index,
                 plan_key, name, files, urns, action_body, verify_cmd,
                 done_criterion, confidence_basis, wave, depends_on,
                 assumption_ids, status)
            VALUES
                (:id, :p, :a, 0,
                 'T1', :name,
                 CAST(:files AS JSONB), CAST(:urns AS JSONB),
                 :action, :vc, :dc,
                 CAST('[]' AS JSONB), 1, CAST('[]' AS JSONB),
                 CAST('[]' AS JSONB),
                 'approved')
            """
        ),
        {
            "id": pid,
            "p": phase_id,
            "a": artifact_id,
            "name": str(task.get("name") or "(unnamed)")[:400],
            "files": json.dumps(task.get("files") or []),
            "urns": json.dumps(task.get("urns") or []),
            "action": str(task.get("action_body") or "")[:8000],
            "vc": str(task.get("verify_cmd") or "") or None,
            "dc": str(task.get("done_criterion") or "")[:1000],
        },
    )
    return pid
