"""Execution orchestrator — walks a task's approved plans wave-by-wave.

Wave-parallel execution (Layer 7)
---------------------------------
Plans with the same ``wave`` run concurrently, bounded by a semaphore
(default 3). Wave N+1 only starts once every wave-N task has settled.
A shared ``asyncio.Lock`` serializes the final ``git add/commit`` step
across the concurrent executors so the git index isn't written from
multiple processes at once.

Stop-on-first-failure
---------------------
In a wave: let in-flight tasks finish, then don't start the next wave.
In the current wave, other tasks already launched keep running — we
don't cancel them because partial work may still be useful, and
cancelling mid-commit would leave the worktree in a messy state.

Lifecycle
---------
* Before: task.state must be ``plan_ready`` or ``executing`` (resume)
  or ``blocked`` (retry).
* During: state = ``executing``.
* After all complete: state = ``verifying`` (Layer 7 verifier picks up).
* After any fail: state = ``blocked``.

Idempotent: re-invoking skips already-completed / rejected plans.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.actions.executor import PlanOutcome, execute_plan
from bifrost.actions.task_lifecycle import TaskState, record_event, transition
from bifrost.actions.worktree import ensure_for_task
from bifrost.api.deps import get_session_factory
from bifrost.cli.config import BifrostSettings, get_settings

logger = logging.getLogger(__name__)


# Within a wave: max concurrent plan executors per task. Bounded low
# because each spawns a Claude CLI subprocess (each ~1 session slot).
DEFAULT_WAVE_CONCURRENCY = 3


@dataclass
class ExecutionRunResult:
    task_id: str
    worktree_path: str
    branch: str
    plans_executed: int
    plans_completed: int
    plans_failed: int
    plans_skipped: int
    total_cost_usd: float
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    stopped_at_plan: str | None = None
    final_task_state: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Entry point ─────────────────────────────────────────────────────


async def execute_task(
    session: AsyncSession,
    *,
    task_id: str,
    target_repo_path: str,
    settings: BifrostSettings | None = None,
    resume: bool = True,
    wave_concurrency: int = DEFAULT_WAVE_CONCURRENCY,
) -> ExecutionRunResult:
    """Walk every approved plan for a task, wave-by-wave (parallel within a wave)."""
    settings = settings or get_settings()

    # Lifecycle guard
    task = (
        await session.execute(
            text("SELECT state FROM tasks WHERE id = :id"),
            {"id": task_id},
        )
    ).first()
    if task is None:
        raise ValueError(f"task {task_id} not found")
    bstate = TaskState(task[0])
    if bstate not in (
        TaskState.PLAN_READY,
        TaskState.EXECUTING,
        TaskState.BLOCKED,
    ):
        raise ValueError(
            f"task is {bstate.value}; cannot execute from this state. Approve the plan first.",
        )

    # Worktree
    worktree = await ensure_for_task(
        session,
        task_id=task_id,
        target_repo_path=target_repo_path,
    )
    await session.commit()

    # Move task → EXECUTING if not already
    if bstate == TaskState.PLAN_READY:
        await transition(
            session,
            task_id=task_id,
            to_state=TaskState.EXECUTING,
            actor="user",
            reason="execution started",
        )
        await session.commit()

    # Pull every approved plan across all phases in this task
    rows = (
        await session.execute(
            text(
                """
                SELECT p.id::text, p.plan_key, p.wave, p.status
                FROM task_plans p
                JOIN task_phases ph ON ph.id = p.phase_id
                WHERE ph.task_id = :b
                ORDER BY p.wave ASC, p.order_index ASC
                """
            ),
            {"b": task_id},
        )
    ).all()

    result = ExecutionRunResult(
        task_id=task_id,
        worktree_path=worktree.path,
        branch=worktree.branch,
        plans_executed=0,
        plans_completed=0,
        plans_failed=0,
        plans_skipped=0,
        total_cost_usd=0.0,
    )

    stop_because_failed = False

    # Group by wave preserving order
    waves: dict[int, list[tuple[str, str, str]]] = {}
    for pid, pkey, pwave, pstatus in rows:
        waves.setdefault(int(pwave), []).append((pid, pkey, pstatus))

    # Shared across all concurrent executors in this task run.
    commit_lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, wave_concurrency))
    sf = get_session_factory(settings)

    for wave in sorted(waves.keys()):
        if stop_because_failed:
            break

        # Decide which plans in this wave need execution. Skip already-
        # completed / rejected ones synchronously (no task spawn needed).
        runnable: list[tuple[str, str, str]] = []
        for pid, pkey, pstatus in waves[wave]:
            if pstatus == "completed":
                result.plans_skipped += 1
                continue
            if pstatus == "rejected":
                result.plans_skipped += 1
                continue
            if pstatus not in ("approved", "executing", "failed"):
                result.plans_skipped += 1
                continue
            if pstatus != "approved" and not resume:
                result.plans_skipped += 1
                continue
            runnable.append((pid, pkey, pstatus))

        if not runnable:
            continue

        # Launch wave-parallel tasks — each with its own DB session so
        # the orchestrator's own session isn't reused concurrently.
        async def _run_one(pid: str, pkey: str) -> PlanOutcome:
            async with sem, sf() as inner_session:
                try:
                    oc = await execute_plan(
                        inner_session,
                        plan_id=pid,
                        worktree=worktree,
                        settings=settings,
                        commit_lock=commit_lock,
                    )
                    return oc
                except Exception as exc:
                    logger.exception(
                        "orchestrator: execute_plan crashed for %s",
                        pid,
                    )
                    oc = PlanOutcome(
                        plan_id=pid,
                        plan_key=pkey,
                        status="failed",
                        git_sha=None,
                        commit_msg=None,
                        output_log=f"orchestrator crash: {exc!r}",
                    )
                    try:
                        await inner_session.execute(
                            text(
                                """
                                    UPDATE task_plans
                                    SET status = 'failed',
                                        output_log = :log
                                    WHERE id = :id
                                    """
                            ),
                            {"id": pid, "log": oc.output_log},
                        )
                        await inner_session.commit()
                    except Exception:
                        logger.exception(
                            "failed to persist crash status for %s",
                            pid,
                        )
                    return oc

        outcomes = await asyncio.gather(
            *(_run_one(pid, pkey) for pid, pkey, _ in runnable),
            return_exceptions=False,
        )

        # Record wave results in DB order (deterministic audit)
        for (pid, pkey, _), outcome in zip(runnable, outcomes, strict=False):
            result.plans_executed += 1
            result.total_cost_usd += outcome.agent_cost_usd
            result.outcomes.append(outcome.to_dict())
            if outcome.status == "completed":
                result.plans_completed += 1
                await record_event(
                    session,
                    task_id=task_id,
                    actor="agent:executor",
                    action="plan_completed",
                    payload={
                        "plan_id": pid,
                        "plan_key": pkey,
                        "git_sha": outcome.git_sha,
                        "files_changed": len(outcome.files_changed),
                        "cost_usd": outcome.agent_cost_usd,
                        "wave": wave,
                    },
                )
            else:
                result.plans_failed += 1
                stop_because_failed = True
                if result.stopped_at_plan is None:
                    result.stopped_at_plan = pkey
                await record_event(
                    session,
                    task_id=task_id,
                    actor="agent:executor",
                    action="plan_failed",
                    payload={
                        "plan_id": pid,
                        "plan_key": pkey,
                        "verify_passed": outcome.verify_passed,
                        "cost_usd": outcome.agent_cost_usd,
                        "wave": wave,
                    },
                )
        await session.commit()

    # Terminal transition
    if result.plans_failed > 0:
        try:
            await transition(
                session,
                task_id=task_id,
                to_state=TaskState.BLOCKED,
                actor="agent:executor",
                reason=f"plan {result.stopped_at_plan} failed",
            )
        except Exception:
            logger.exception("could not transition to BLOCKED")
    else:
        # Every approved plan either completed or was skipped.
        try:
            await transition(
                session,
                task_id=task_id,
                to_state=TaskState.VERIFYING,
                actor="agent:executor",
                reason="all plans completed; entering verify",
            )
        except Exception:
            logger.exception("could not transition to VERIFYING")

    await session.commit()

    result.final_task_state = (
        await session.execute(
            text("SELECT state FROM tasks WHERE id = :id"),
            {"id": task_id},
        )
    ).scalar_one()
    return result
