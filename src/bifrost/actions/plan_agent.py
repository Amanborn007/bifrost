"""Plan agent (Layer 5) — turns an approved context into atomic tasks.

The plan agent consumes:
    * the approved ``context`` artifact for a phase
    * the active ``task_assumptions`` attached to that artifact
    * any prior plan artifact (for re-planning after a cascade)

and emits:
    * a new ``plan`` artifact with body_md = readable plan overview
    * N atomic ``task_plans`` rows, each with files/action/verify/done
    * M ``task_verify_items`` rows (conversational acceptance checks)
    * ``task_assumption_uses`` rows linking the plan artifact to every
      assumption it actually cited (by key)

Versioning mirrors the discussion agent: each run writes a fresh
artifact with version = prior + 1. Old task_plans rows from earlier
versions are left as-is — their ``status`` column already tracks their
fate, and the cascade module will flip pending ones to 'rejected' if
their underlying assumptions change.

Assumption_ids column
---------------------
``task_plans.assumption_ids`` is a JSONB array of assumption UUIDs.
The LLM gives us assumption KEYS ("A1", "A5"); we resolve them to UUIDs
before the insert by reading the active assumptions for the current
artifact. Keys the LLM invented that don't exist are dropped with a
warning — we never fabricate a UUID.
"""

from __future__ import annotations

import json
import logging
import time
import uuid as _uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.actions import tools as _tools
from bifrost.actions._prompt_safety import BOUNDARY_SENTENCE, wrap_untrusted
from bifrost.actions._shell_safe import validate_verify_cmd
from bifrost.actions.task_lifecycle import TaskState, record_event, transition
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient

logger = logging.getLogger(__name__)


# ── JSON schema fed to Claude ────────────────────────────────────────


_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["plan_md", "tasks", "verify_items", "ready_to_execute"],
    "properties": {
        "plan_md": {
            "type": "string",
            "description": (
                "Readable plan overview: ## Overview, ## Sequencing, ## Risks. ≤ 80 lines."
            ),
        },
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "plan_key",
                    "name",
                    "files",
                    "action_body",
                    "done_criterion",
                    "wave",
                ],
                "properties": {
                    "plan_key": {
                        "type": "string",
                        "description": "Stable key like 'T1', 'T2'...",
                    },
                    "name": {"type": "string"},
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Repo-relative paths touched by this task.",
                    },
                    "urns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "URNs of entities the task operates on.",
                    },
                    "action_body": {
                        "type": "string",
                        "description": "Concrete instructions for the executor agent.",
                    },
                    "verify_cmd": {
                        "type": "string",
                        "description": (
                            "Single shell command the verifier runs to check "
                            "this task passed. Empty = conversational verify only."
                        ),
                    },
                    "done_criterion": {
                        "type": "string",
                        "description": "Human-readable sentence: what 'done' looks like.",
                    },
                    "wave": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Parallel-execution wave; tasks with same wave "
                            "may run concurrently. Later waves wait."
                        ),
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "plan_keys this task waits on.",
                    },
                    "assumption_keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Keys (A1, A5...) of assumptions this task "
                            "relies on. Critical for correction cascade."
                        ),
                    },
                    "confidence_basis": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Tier strings like 'T1 code_verified' or "
                            "'T5 llm_guess' showing WHY we believe this "
                            "task is ready to run."
                        ),
                    },
                },
            },
        },
        "verify_items": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Task-wide acceptance checks stated in plain language. "
                "Each becomes a task_verify_items row and is graded "
                "conversationally at end of execute phase."
            ),
        },
        "ready_to_execute": {
            "type": "boolean",
            "description": (
                "True when every task cites real files and real "
                "assumptions — nothing important is still a guess. "
                "False = needs another discussion round first."
            ),
        },
    },
}


_PLAN_SYSTEM_PROMPT = """\
You are Bifrost's plan agent. You take an APPROVED shared-understanding
(context.md + typed assumptions) and produce an atomic execution plan.

HARD RULES:

1. TASKS ARE ATOMIC. Each task is one concrete action that an engineer
   (or executor agent) does in one sitting: edit these files, run this
   command, check this output. "Migrate the cache" is NOT a task;
   "Create the order_status_cache table" and "Wire the Postgres
   reader into OrderService.get_status" are.
2. EVERY TASK CITES ASSUMPTIONS IT DEPENDS ON. Pull the A-keys from
   the provided assumption list. If a task assumes something that's
   NOT in the list, say so in the plan_md under ## Risks AND lower
   that task's wave (so it runs after a discussion round upgrades
   the assumption).
3. WAVES ENABLE PARALLELISM. wave=1 tasks have no inter-dependencies
   and can run concurrently. wave=2 tasks depend on wave=1 results.
   Use depends_on for explicit task-level ordering. Prefer lower
   wave numbers — they're cheaper.
4. VERIFY_CMD IS A SINGLE SHELL COMMAND OR EMPTY.
   Good: `alembic upgrade head`, `pytest tests/test_orders.py -q`,
   empty string (falls back to conversational verify).
   Bad: multi-line shell, bash one-liners with subshells, curl pipelines.

   VERIFY_CMD MUST NOT DEPEND ON T4/T5 GUESSED FILENAMES. If the task's
   confidence_basis contains any tier-4 or tier-5 entries that mention a
   filename, path, or test module, use an *assumption-safe* verify_cmd
   instead of pinning that name:
     - Prefer `pytest -v` or `pytest tests/ -q` over
       `pytest path/that/might/not/exist.py -v`.
     - Prefer `grep -rn PATTERN . --include='*.py'` over
       `grep PATTERN file_we_guessed.py`.
     - Prefer `python -c "import pkg; ..."` over a made-up script path.
   The discovery task (wave 1) will confirm the real names; downstream
   verify_cmds must tolerate whatever filename the audit finds. If no
   assumption-safe command is possible, leave verify_cmd empty and rely
   on conversational verify in Layer 7.
5. DONE_CRITERION IS A SENTENCE, not a command. It describes what the
   human will see to agree "yes, this task is complete."
6. TOUCH ONLY FILES THAT PLAUSIBLY EXIST (or that this task creates).
   Don't drag in files just to pad the blast radius.
7. VERIFY_ITEMS ARE TASK-WIDE acceptance checks — "All existing tests
   pass unchanged", "No new warnings in CI", "p99 latency comparison
   shows Postgres within 10% of baseline." These are conversational
   in Layer 7+.
8. READY_TO_EXECUTE=false IS ALWAYS OK. If the context has unanswered
   blocker questions, or critical assumptions are tier 4/5 guesses,
   return false and explain in plan_md under ## Risks.

Assume a senior engineer will execute this. Be surgical; don't pad.
No ASCII diagrams in plan_md (we have Mermaid elsewhere — keep the
plan text).

__BIFROST_BOUNDARY_SENTINEL__

Respond with ONLY the JSON object the schema expects.
""".replace("__BIFROST_BOUNDARY_SENTINEL__", BOUNDARY_SENTENCE)


# ── Result type ──────────────────────────────────────────────────────


@dataclass
class PlanResult:
    task_id: str
    phase_id: str
    artifact_id: str
    artifact_version: int
    ready_to_execute: bool
    plan_md: str
    tasks_written: int
    verify_items_written: int
    assumption_links: int
    skipped_unknown_keys: list[str]
    duration_ms: int
    cost_usd: float
    ask_back_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Entry point ──────────────────────────────────────────────────────


async def run_plan(
    session: AsyncSession,
    *,
    task_id: str,
    phase_id: str | None = None,
    user_message: str | None = None,
    settings: BifrostSettings | None = None,
) -> PlanResult:
    """Produce a new plan artifact + task_plans rows for a phase."""
    started = time.monotonic()
    settings = settings or get_settings()

    # 1. Task + phase
    task = (
        await session.execute(
            text("SELECT id, title, intent_text, state FROM tasks WHERE id = :id"),
            {"id": task_id},
        )
    ).first()
    if task is None:
        raise ValueError(f"Task {task_id} not found")

    if phase_id is None:
        row = (
            await session.execute(
                text("SELECT id FROM task_phases WHERE task_id = :b ORDER BY order_index LIMIT 1"),
                {"b": task_id},
            )
        ).first()
        if row is None:
            raise ValueError("Task has no phases — run /discuss first to create one.")
        phase_id = str(row[0])

    # 2. Latest context artifact (must exist)
    ctx = await _load_latest_context(session, phase_id)
    if ctx is None:
        raise ValueError("No context artifact yet. Run /discuss before /plan.")

    # 3. Active assumptions for that artifact + alias map (key → id)
    active_assumptions = await _load_active_assumptions(
        session,
        ctx["id"],
    )
    key_to_id = {a["key"]: a["id"] for a in active_assumptions}

    # 4. Latest prior plan (for re-plan context; optional)
    prior_plan = await _load_latest_plan_artifact(session, phase_id)
    prior_version = prior_plan["version"] if prior_plan else 0

    # 5. Build user prompt + call Claude
    user_prompt = _build_user_prompt(
        title=task[1],
        intent_text=task[2],
        context_md=ctx["body_md"],
        assumptions=active_assumptions,
        prior_plan_md=prior_plan["body_md"] if prior_plan else None,
        user_message=user_message,
    )

    client = ClaudeLlmClient(settings)
    try:
        completion = await client.call_with_json_schema(
            system_prompt=_PLAN_SYSTEM_PROMPT,
            user_message=user_prompt,
            model=settings.agent_model,  # Sonnet
            json_schema=_PLAN_SCHEMA,
            max_turns=1,
            timeout_override=240,
        )
    except ClaudeCliError:
        logger.exception("plan agent LLM call failed")
        raise

    data = completion.parsed or {}
    usage = completion.usage
    cost = float(getattr(usage, "total_cost_usd", 0.0) or 0.0)

    # 6. Write the artifact
    new_version = prior_version + 1
    artifact_id = await _insert_plan_artifact(
        session,
        phase_id=phase_id,
        version=new_version,
        body_md=data.get("plan_md", ""),
        parsed={
            "ready_to_execute": bool(data.get("ready_to_execute", False)),
            "task_count": len(data.get("tasks", []) or []),
            "verify_item_count": len(data.get("verify_items", []) or []),
        },
    )

    # 7. task_plans rows + assumption_uses links
    tasks = data.get("tasks") or []
    written = 0
    used_assumption_ids: set[str] = set()
    skipped: list[str] = []
    for idx, task_payload in enumerate(tasks):
        resolved_ids, unknown = _resolve_assumption_keys(
            task_payload.get("assumption_keys") or [],
            key_to_id,
        )
        skipped.extend(unknown)
        used_assumption_ids.update(resolved_ids)
        await _insert_plan_row(
            session,
            phase_id=phase_id,
            artifact_id=artifact_id,
            order_index=idx,
            task=task_payload,
            assumption_ids=resolved_ids,
        )
        written += 1

    # 8. Verify items
    v_items = data.get("verify_items") or []
    vi_written = 0
    for idx, v in enumerate(v_items):
        await session.execute(
            text(
                """
                INSERT INTO task_verify_items
                    (id, phase_id, plan_id, order_index, text, status)
                VALUES
                    (:id, :p, NULL, :idx, :t, 'pending')
                """
            ),
            {
                "id": str(_uuid.uuid4()),
                "p": phase_id,
                "idx": idx,
                "t": str(v)[:4000],
            },
        )
        vi_written += 1

    # 9. task_assumption_uses (artifact-level link set)
    for aid in used_assumption_ids:
        await session.execute(
            text(
                """
                INSERT INTO task_assumption_uses
                    (artifact_id, assumption_id)
                VALUES (:a, :u)
                ON CONFLICT DO NOTHING
                """
            ),
            {"a": artifact_id, "u": aid},
        )

    # 10. Lifecycle: DISCUSS_DONE → DRAFTING_PLAN if applicable
    current = TaskState(task[3])
    if current in (TaskState.DISCUSS_DONE, TaskState.DISCUSSING):
        try:
            if current == TaskState.DISCUSSING:
                await transition(
                    session,
                    task_id=task_id,
                    to_state=TaskState.DISCUSS_DONE,
                    actor="agent:plan",
                    reason="plan agent auto-advancing from DISCUSSING",
                )
            await transition(
                session,
                task_id=task_id,
                to_state=TaskState.DRAFTING_PLAN,
                actor="agent:plan",
                reason="plan agent produced plan artifact",
            )
        except Exception:
            logger.exception("plan lifecycle advance failed")

    # 11. Phase.current_step = 'plan'
    await session.execute(
        text("UPDATE task_phases SET current_step = 'plan', updated_at = NOW() WHERE id = :p"),
        {"p": phase_id},
    )

    # 12. Audit ask_back row + event
    duration_ms = int((time.monotonic() - started) * 1000)
    ab_id = await _tools._insert_ask_back(
        session,
        task_id=task_id,
        phase_id=phase_id,
        agent_kind="plan",
        question="(plan round)",
        scope={
            "user_message": user_message or "",
            "new_version": new_version,
            "tasks": written,
            "verify_items": vi_written,
        },
        answered_from="research_pipeline",
        answer_summary=(data.get("plan_md") or "")[:800],
        research_tokens=int(getattr(usage, "input_tokens", 0) + getattr(usage, "output_tokens", 0)),
        research_cost=Decimal(str(cost)),
        duration_ms=duration_ms,
        answer_confidence=(Decimal("0.85") if data.get("ready_to_execute") else Decimal("0.55")),
    )

    await record_event(
        session,
        task_id=task_id,
        actor="agent:plan",
        action="plan_round",
        payload={
            "artifact_id": artifact_id,
            "version": new_version,
            "tasks": written,
            "verify_items": vi_written,
            "ready_to_execute": bool(data.get("ready_to_execute")),
            "cost_usd": cost,
            "skipped_unknown_keys": skipped,
        },
    )

    return PlanResult(
        task_id=task_id,
        phase_id=phase_id,
        artifact_id=artifact_id,
        artifact_version=new_version,
        ready_to_execute=bool(data.get("ready_to_execute", False)),
        plan_md=data.get("plan_md", ""),
        tasks_written=written,
        verify_items_written=vi_written,
        assumption_links=len(used_assumption_ids),
        skipped_unknown_keys=skipped,
        duration_ms=duration_ms,
        cost_usd=cost,
        ask_back_id=ab_id,
    )


# ── Approval helpers (used by the /approve endpoint) ─────────────────


async def approve_plan_artifact(
    session: AsyncSession,
    *,
    task_id: str,
    artifact_id: str,
) -> dict[str, Any]:
    """Mark a plan artifact approved + flip its task_plans rows to 'approved'.

    Transitions lifecycle DRAFTING_PLAN → PLAN_READY.
    """
    now = datetime.now(UTC)
    row = (
        await session.execute(
            text("SELECT kind, state FROM task_artifacts WHERE id = :id"),
            {"id": artifact_id},
        )
    ).first()
    if row is None:
        raise ValueError(f"artifact {artifact_id} not found")
    kind, state = row[0], row[1]
    if kind != "plan":
        raise ValueError(f"artifact is {kind}, not a plan")
    if state == "approved":
        return {"already_approved": True}

    await session.execute(
        text(
            """
            UPDATE task_artifacts
            SET state = 'approved', approved_at = :now
            WHERE id = :id
            """
        ),
        {"id": artifact_id, "now": now},
    )
    # Flip pending plans derived from this artifact
    await session.execute(
        text(
            """
            UPDATE task_plans
            SET status = 'approved'
            WHERE derived_from_artifact_id = :a AND status = 'pending'
            """
        ),
        {"a": artifact_id},
    )
    # Lifecycle: DRAFTING_PLAN → PLAN_READY
    bstate = (
        await session.execute(
            text("SELECT state FROM tasks WHERE id = :id"),
            {"id": task_id},
        )
    ).scalar_one_or_none()
    if bstate == TaskState.DRAFTING_PLAN.value:
        try:
            await transition(
                session,
                task_id=task_id,
                to_state=TaskState.PLAN_READY,
                actor="user",
                reason="plan approved",
            )
        except Exception:
            logger.exception("could not advance task to PLAN_READY")
    await record_event(
        session,
        task_id=task_id,
        actor="user",
        action="artifact_approved",
        payload={"artifact_id": artifact_id, "kind": "plan"},
    )
    return {"approved_at": now.isoformat()}


async def approve_context_artifact(
    session: AsyncSession,
    *,
    task_id: str,
    artifact_id: str,
) -> dict[str, Any]:
    """Mark a context artifact approved → DISCUSSING → DISCUSS_DONE."""
    now = datetime.now(UTC)
    row = (
        await session.execute(
            text("SELECT kind, state FROM task_artifacts WHERE id = :id"),
            {"id": artifact_id},
        )
    ).first()
    if row is None:
        raise ValueError(f"artifact {artifact_id} not found")
    kind = row[0]
    if kind != "context":
        raise ValueError(f"artifact is {kind}, not a context")
    if row[1] == "approved":
        return {"already_approved": True}

    await session.execute(
        text(
            """
            UPDATE task_artifacts
            SET state = 'approved', approved_at = :now
            WHERE id = :id
            """
        ),
        {"id": artifact_id, "now": now},
    )

    bstate = (
        await session.execute(
            text("SELECT state FROM tasks WHERE id = :id"),
            {"id": task_id},
        )
    ).scalar_one_or_none()
    if bstate == TaskState.DISCUSSING.value:
        try:
            await transition(
                session,
                task_id=task_id,
                to_state=TaskState.DISCUSS_DONE,
                actor="user",
                reason="context approved",
            )
        except Exception:
            logger.exception("could not advance task to DISCUSS_DONE")
    await record_event(
        session,
        task_id=task_id,
        actor="user",
        action="artifact_approved",
        payload={"artifact_id": artifact_id, "kind": "context"},
    )
    return {"approved_at": now.isoformat()}


# ── DB helpers ──────────────────────────────────────────────────────


async def _load_latest_context(
    session: AsyncSession,
    phase_id: str,
) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                """
                SELECT id, version, body_md, state
                FROM task_artifacts
                WHERE phase_id = :p AND kind = 'context'
                ORDER BY version DESC LIMIT 1
                """
            ),
            {"p": phase_id},
        )
    ).first()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "version": int(row[1]),
        "body_md": row[2] or "",
        "state": row[3],
    }


async def _load_latest_plan_artifact(
    session: AsyncSession,
    phase_id: str,
) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                """
                SELECT id, version, body_md, state
                FROM task_artifacts
                WHERE phase_id = :p AND kind = 'plan'
                ORDER BY version DESC LIMIT 1
                """
            ),
            {"p": phase_id},
        )
    ).first()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "version": int(row[1]),
        "body_md": row[2] or "",
        "state": row[3],
    }


async def _load_active_assumptions(
    session: AsyncSession,
    artifact_id: str,
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT id::text, key, value, source_type, confidence, tier
                FROM task_assumptions
                WHERE artifact_id = :a AND state = 'active'
                ORDER BY key ASC
                """
            ),
            {"a": artifact_id},
        )
    ).all()
    return [
        {
            "id": r[0],
            "key": r[1],
            "value": r[2],
            "source_type": r[3],
            "confidence": float(r[4]),
            "tier": int(r[5]),
        }
        for r in rows
    ]


async def _insert_plan_artifact(
    session: AsyncSession,
    *,
    phase_id: str,
    version: int,
    body_md: str,
    parsed: dict[str, Any],
) -> str:
    aid = str(_uuid.uuid4())
    await session.execute(
        text(
            """
            INSERT INTO task_artifacts
                (id, phase_id, kind, version, body_md, state,
                 parsed, produced_by_agent)
            VALUES
                (:id, :p, 'plan', :v, :body, 'draft',
                 CAST(:parsed AS JSONB), 'plan_agent')
            """
        ),
        {
            "id": aid,
            "p": phase_id,
            "v": version,
            "body": body_md,
            "parsed": json.dumps(parsed),
        },
    )
    return aid


def _safe_verify_cmd(task: dict[str, Any]) -> str | None:
    """Validate the LLM-supplied verify_cmd before persistence.

    Returns the original cmd if safe, ``None`` if empty, and ``None``
    (with a warning logged) if rejected. The plan row then has no
    ``verify_cmd`` and the executor falls back to conversational verify.
    """
    raw = str(task.get("verify_cmd") or "")
    if not raw.strip():
        return None
    ok, reason = validate_verify_cmd(raw)
    if not ok:
        logger.warning(
            "plan_agent: rejected unsafe verify_cmd for task %s: %s — %r",
            task.get("plan_key"),
            reason,
            raw,
        )
        return None
    return raw


async def _insert_plan_row(
    session: AsyncSession,
    *,
    phase_id: str,
    artifact_id: str,
    order_index: int,
    task: dict[str, Any],
    assumption_ids: list[str],
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
                (:id, :p, :a, :o, :key, :name,
                 CAST(:files AS JSONB), CAST(:urns AS JSONB),
                 :ab, :vc, :dc,
                 CAST(:conf AS JSONB), :wave, CAST(:dep AS JSONB),
                 CAST(:aids AS JSONB), 'pending')
            """
        ),
        {
            "id": pid,
            "p": phase_id,
            "a": artifact_id,
            "o": order_index,
            "key": str(task.get("plan_key") or f"T{order_index + 1}")[:64],
            "name": str(task.get("name") or "(unnamed task)")[:400],
            "files": json.dumps(task.get("files") or []),
            "urns": json.dumps(task.get("urns") or []),
            "ab": str(task.get("action_body") or "")[:8000],
            # Validate verify_cmd before persistence. Reject = blank
            # (falls back to conversational verify) so the executor never
            # sees an unvalidated shell command. The reason is logged so a
            # later UI surface can show "verify_cmd rejected: …".
            "vc": _safe_verify_cmd(task),
            "dc": str(task.get("done_criterion") or "")[:1000],
            "conf": json.dumps(task.get("confidence_basis") or []),
            "wave": int(task.get("wave") or 1),
            "dep": json.dumps(task.get("depends_on") or []),
            "aids": json.dumps(assumption_ids),
        },
    )
    return pid


def _resolve_assumption_keys(
    keys: list[str],
    key_to_id: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Return (resolved_ids, unknown_keys)."""
    resolved: list[str] = []
    unknown: list[str] = []
    for k in keys or []:
        k = str(k).strip()
        if not k:
            continue
        if k in key_to_id:
            resolved.append(key_to_id[k])
        else:
            unknown.append(k)
    return resolved, unknown


def _build_user_prompt(
    *,
    title: str,
    intent_text: str,
    context_md: str,
    assumptions: list[dict[str, Any]],
    prior_plan_md: str | None,
    user_message: str | None,
) -> str:
    # Every variable below carries attacker-controlled input
    # (intent_text), prior LLM output (context_md, prior_plan_md), or
    # user-typed refinement (user_message). Wrap each in a tagged
    # boundary so the model treats them as data, not instructions.
    parts: list[str] = [
        f"Task title: {title}",
        "",
        "Original intent:",
        wrap_untrusted(intent_text or "(none)", "user_intent"),
        "",
        "Approved context (context.md):",
        wrap_untrusted(context_md or "(empty)", "prior_context"),
    ]
    if assumptions:
        parts.append("")
        parts.append("Active assumptions (use the A-keys when citing):")
        for a in assumptions:
            parts.append(
                f"- {a['key']}: {a['value']} "
                f"[source={a['source_type']} conf={a['confidence']:.2f} "
                f"tier={a['tier']}]"
            )
    if prior_plan_md:
        parts.append("")
        parts.append("Prior plan (will be superseded; use for continuity):")
        parts.append(wrap_untrusted(prior_plan_md, "prior_plan"))
    if user_message:
        parts.append("")
        parts.append("User note for this round:")
        parts.append(wrap_untrusted(user_message, "user_message"))
    parts.append("")
    parts.append("Produce the plan JSON per the schema.")
    return "\n".join(parts)
