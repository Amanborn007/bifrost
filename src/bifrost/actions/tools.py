"""Agent-facing ask-back tools.

The three callables here are what an agent invokes whenever it hits a
knowledge gap while working on a task. Conceptually they are the
"phone a friend" layer: instead of hallucinating, the agent says
``bifrost_research(...)`` / ``bifrost_trigger_extraction(...)`` /
``bifrost_ask_user(...)`` and lets Bifrost either answer from the
graph, grow the graph, or escalate to the human.

Each tool:
  1. Calls ``budget.check()`` first — if the task has blown its cap
     we short-circuit and record an ``ask_backs`` row with
     ``answered_from='budget_exceeded'``.
  2. Does the work (research pipeline / task creation /
     task_clarifications insert).
  3. Writes audit rows so Layer 4 (continuous learning) has a clean
     trail of what the agent asked, where the answer came from, and
     what it cost.

These functions are pure I/O — they don't kick off async tasks that
outlive the caller. Long-running background work (extraction) goes
through the existing ``ExtractionWorker`` which the caller can poll
via ``extraction_task_id``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid as _uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bifrost.actions import budget as _budget
from bifrost.actions.gap_detector import GapAssessment, assess_coverage
from bifrost.cli.config import BifrostSettings

logger = logging.getLogger(__name__)


# ── Result types ──────────────────────────────────────────────────────


ResearchSource = Literal[
    "graph_only",  # answered from graph without research pipeline
    "research_pipeline",  # full run_research call
    "extraction_then_research",  # triggered extraction then researched
    "budget_exceeded",  # short-circuited
]


@dataclass
class ResearchResult:
    ask_back_id: str
    answered_from: ResearchSource
    answer: str
    confidence: str | None
    sources: list[dict[str, Any]]
    usage: dict[str, Any]
    gap_assessment: dict[str, Any] | None
    duration_ms: int
    cost_usd: float
    budget_remaining: dict[str, Any]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExtractionTriggerResult:
    trigger_id: str
    extraction_task_ids: list[str]
    scope_repo_names: list[str]
    mode: str  # shallow | deep
    trigger_kind: str
    status: str  # "queued" | "running" | "done" | "partial" | "failed" | "budget_exceeded"
    waited_ms: int
    tasks_status: dict[str, str]  # task_id → terminal status (pending / running / done / failed)
    budget_remaining: dict[str, Any]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ClarificationResult:
    clarification_id: str
    state: str  # open | answered | ...
    question: str
    options: list[dict[str, Any]]
    blocks_state: str | None
    kind: str

    def to_dict(self) -> dict:
        return asdict(self)


# ── bifrost_research ──────────────────────────────────────────────────


async def bifrost_research(
    session: AsyncSession,
    *,
    task_id: str,
    question: str,
    agent_kind: str,  # "discuss" | "plan" | "execute" | "verify" | "coordinator" | ...
    scope_repo_ids: list[str] | None = None,
    phase_id: str | None = None,
    conversation_id: str | None = None,
    settings: BifrostSettings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    gap_aware: bool = True,
    suggest_extractions: bool = True,
) -> ResearchResult:
    """Run research for an agent; always records an audit row.

    ``gap_aware=True``: do a coverage assessment first; if verdict is
    ``insufficient``, we return early with the gap report instead of
    spending on the full research pipeline. The caller then decides
    whether to trigger extraction via ``bifrost_trigger_extraction``.

    When ``session_factory`` + ``settings`` are provided we run the
    real ``run_research`` pipeline. If omitted (common in unit tests),
    we fall back to a graph-only synthesis summary.
    """
    started = time.monotonic()

    # 1. Budget guard — raises BudgetExceeded
    try:
        snap = await _budget.check(session, task_id, need_ask_back=True)
    except _budget.BudgetExceeded as e:
        ab_id = await _insert_ask_back(
            session,
            task_id=task_id,
            phase_id=phase_id,
            agent_kind=agent_kind,
            question=question,
            scope={"repo_ids": scope_repo_ids or []},
            answered_from="budget_exceeded",
            answer_summary=str(e),
            research_tokens=0,
            research_cost=Decimal(0),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        snap = await _budget.snapshot(session, task_id)
        return ResearchResult(
            ask_back_id=ab_id,
            answered_from="budget_exceeded",
            answer=str(e),
            confidence=None,
            sources=[],
            usage={},
            gap_assessment=None,
            duration_ms=int((time.monotonic() - started) * 1000),
            cost_usd=0.0,
            budget_remaining=snap.to_dict(),
        )

    # 2. Optional coverage check
    gap: GapAssessment | None = None
    if gap_aware:
        gap = await assess_coverage(
            session,
            question=question,
            scope_repo_ids=scope_repo_ids,
            suggest_extractions=suggest_extractions,
        )
        if gap.verdict == "insufficient":
            ab_id = await _insert_ask_back(
                session,
                task_id=task_id,
                phase_id=phase_id,
                agent_kind=agent_kind,
                question=question,
                scope={
                    "repo_ids": scope_repo_ids or [],
                    "coverage_score": gap.coverage_score,
                },
                answered_from="graph_only",
                answer_summary=(
                    f"Graph coverage insufficient (score {gap.coverage_score:.2f}); "
                    f"{len(gap.suggested_repos_to_extract)} repo(s) suggested for extraction."
                ),
                research_tokens=0,
                research_cost=Decimal(0),
                duration_ms=int((time.monotonic() - started) * 1000),
                answer_confidence=Decimal("0.20"),
            )
            snap = await _budget.snapshot(session, task_id)
            return ResearchResult(
                ask_back_id=ab_id,
                answered_from="graph_only",
                answer=(
                    "I don't have enough in the graph to answer that "
                    "confidently. Consider triggering extraction for "
                    "the suggested repos first."
                ),
                confidence="uncertain",
                sources=[],
                usage={},
                gap_assessment=gap.to_dict(),
                duration_ms=int((time.monotonic() - started) * 1000),
                cost_usd=0.0,
                budget_remaining=snap.to_dict(),
            )

    # 3. Run the real research pipeline if we have deps; else graph-only.
    if settings is not None and session_factory is not None:
        research_payload = await _run_research_pipeline(
            settings=settings,
            session_factory=session_factory,
            question=question,
            scope_repo_ids=scope_repo_ids,
            conversation_id=conversation_id,
        )
        answered_from: ResearchSource = "research_pipeline"
    else:
        research_payload = _graph_only_summary(gap)
        answered_from = "graph_only"

    cost_usd = Decimal(str(research_payload.get("usage", {}).get("cost", 0.0)))
    tokens = int(
        research_payload.get("usage", {}).get("input", 0)
        + research_payload.get("usage", {}).get("output", 0)
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    conf = research_payload.get("confidence")

    ab_id = await _insert_ask_back(
        session,
        task_id=task_id,
        phase_id=phase_id,
        agent_kind=agent_kind,
        question=question,
        scope={"repo_ids": scope_repo_ids or []},
        answered_from=answered_from,
        answer_summary=(research_payload.get("answer") or "")[:800],
        research_tokens=tokens,
        research_cost=cost_usd,
        duration_ms=duration_ms,
        answer_confidence=_confidence_to_decimal(conf),
    )
    snap = await _budget.snapshot(session, task_id)
    return ResearchResult(
        ask_back_id=ab_id,
        answered_from=answered_from,
        answer=research_payload.get("answer", ""),
        confidence=conf,
        sources=research_payload.get("sources", []) or [],
        usage=research_payload.get("usage", {}),
        gap_assessment=gap.to_dict() if gap else None,
        duration_ms=duration_ms,
        cost_usd=float(cost_usd),
        budget_remaining=snap.to_dict(),
    )


# ── bifrost_trigger_extraction ────────────────────────────────────────


async def bifrost_trigger_extraction(
    session: AsyncSession,
    *,
    task_id: str,
    scope_repo_names: list[str],
    mode: Literal["shallow", "deep"] = "shallow",
    trigger_kind: Literal[
        "auto_manifest_only", "ask_back_gap", "user_requested", "scheduled"
    ] = "ask_back_gap",
    phase_id: str | None = None,
    clarification_id: str | None = None,
    wait_seconds: int = 0,
    max_files: int | None = None,
    force_full: bool = False,
    dry_run: bool = False,
) -> ExtractionTriggerResult:
    """Queue extraction for one or more repos and record the trigger.

    When ``wait_seconds > 0`` we poll the tasks table until every task
    reaches a terminal status or the timeout fires. Returns with the
    partial status either way — the caller decides whether to retry.
    """
    started = time.monotonic()

    # 1. Budget guard
    try:
        snap = await _budget.check(session, task_id, need_extraction=True)
    except _budget.BudgetExceeded:
        snap = await _budget.snapshot(session, task_id)
        return ExtractionTriggerResult(
            trigger_id="",
            extraction_task_ids=[],
            scope_repo_names=scope_repo_names,
            mode=mode,
            trigger_kind=trigger_kind,
            status="budget_exceeded",
            waited_ms=int((time.monotonic() - started) * 1000),
            tasks_status={},
            budget_remaining=snap.to_dict(),
        )

    # 2. Resolve repo names → repo rows (register new ones from org_repos
    #    if they exist but aren't yet repos).
    repo_map = await _resolve_or_register_repos(session, scope_repo_names)
    if not repo_map:
        return ExtractionTriggerResult(
            trigger_id="",
            extraction_task_ids=[],
            scope_repo_names=scope_repo_names,
            mode=mode,
            trigger_kind=trigger_kind,
            status="failed",
            waited_ms=int((time.monotonic() - started) * 1000),
            tasks_status={},
            budget_remaining=snap.to_dict(),
        )

    # 3. Create tasks + extraction_triggers rows (one per repo for
    #    clarity; a single trigger row with multiple scope_repos would
    #    have been fine, but per-repo lets us track per-repo failure).
    task_ids: list[str] = []
    trigger_id = str(_uuid.uuid4())  # one logical trigger for this call
    for _name, repo_row in repo_map.items():
        tid = await _create_extraction_task(
            session,
            repo_id=repo_row["id"],
            repo_name=repo_row["name"],
            mode=mode,
            max_files=max_files,
            force_full=force_full,
            dry_run=dry_run,
        )
        task_ids.append(tid)
        await session.execute(
            text(
                """
                INSERT INTO extraction_triggers
                    (id, extraction_task_id, trigger_kind, task_id,
                     phase_id, clarification_id, scope_repos, mode,
                     created_at)
                VALUES
                    (:id, :extraction_task_id, :kind, :task_id, :phase_id,
                     :clar_id, CAST(:scope AS JSONB), :mode, :at)
                """
            ),
            {
                "id": str(_uuid.uuid4()),
                "extraction_task_id": tid,
                "kind": trigger_kind,
                "task_id": task_id,
                "phase_id": phase_id,
                "clar_id": clarification_id,
                "scope": json.dumps([repo_row["name"]]),
                "mode": mode,
                "at": datetime.now(UTC),
            },
        )

    # 4. Submit to worker (fire-and-forget). MUST commit FIRST so the
    # worker's separate session can see the task + trigger rows we
    # just inserted. Without the explicit commit here the worker
    # raised ``ValueError: Task <id> not found`` when it raced ahead
    # of the request handler's outer ``get_db`` commit. Confirmed via
    # an earlier team-name turn (server logs: "Task ... not
    # found" tracebacks for every auto-extracted manifest-only repo).
    if not dry_run:
        await session.commit()
        from bifrost.extraction.worker import get_worker

        worker = get_worker()
        for tid in task_ids:
            worker.submit(tid)

    # 5. Optional wait
    tasks_status: dict[str, str] = {t: "pending" for t in task_ids}
    overall = "queued"
    if wait_seconds > 0 and not dry_run:
        overall, tasks_status = await _wait_for_tasks(
            session,
            task_ids,
            timeout_s=wait_seconds,
        )

    snap = await _budget.snapshot(session, task_id)
    return ExtractionTriggerResult(
        trigger_id=trigger_id,
        extraction_task_ids=task_ids,
        scope_repo_names=[repo_row["name"] for repo_row in repo_map.values()],
        mode=mode,
        trigger_kind=trigger_kind,
        status=overall,
        waited_ms=int((time.monotonic() - started) * 1000),
        tasks_status=tasks_status,
        budget_remaining=snap.to_dict(),
    )


# ── bifrost_ask_user ──────────────────────────────────────────────────


async def bifrost_ask_user(
    session: AsyncSession,
    *,
    task_id: str,
    question: str,
    options: list[dict[str, Any]] | None = None,
    kind: Literal["need_research", "need_user_input", "conflict", "ambiguity"] = "need_user_input",
    blocks_state: str | None = None,
    phase_id: str | None = None,
    origin_artifact_id: str | None = None,
    source: Literal["agent", "system"] = "agent",
) -> ClarificationResult:
    """Push a question into the clarification queue. Returns the id.

    The agent calling this does *not* wait inline. The task's front-
    end polls ``/tasks/:id/clarifications?state=open`` and surfaces
    open items to the user. When answered, the task_clarifications
    row is updated in place and the agent's retry pass re-reads it.

    Setting ``blocks_state`` moves the task's lifecycle to BLOCKED
    (caller responsibility — see task_lifecycle.transition).
    """
    cid = str(_uuid.uuid4())
    now = datetime.now(UTC)
    await session.execute(
        text(
            """
            INSERT INTO task_clarifications
                (id, task_id, phase_id, origin_artifact_id, source,
                 kind, question, options, blocks_state, state,
                 created_at)
            VALUES
                (:id, :task, :phase, :origin, :source, :kind, :q,
                 CAST(:opts AS JSONB), :blocks, 'open', :at)
            """
        ),
        {
            "id": cid,
            "task": task_id,
            "phase": phase_id,
            "origin": origin_artifact_id,
            "source": source,
            "kind": kind,
            "q": question,
            "opts": json.dumps(options or []),
            "blocks": blocks_state,
            "at": now,
        },
    )
    # Bump the pending counter on the task
    await session.execute(
        text(
            """
            UPDATE tasks
            SET clarifications_pending = clarifications_pending + 1,
                updated_at = :now
            WHERE id = :id
            """
        ),
        {"now": now, "id": task_id},
    )
    return ClarificationResult(
        clarification_id=cid,
        state="open",
        question=question,
        options=options or [],
        blocks_state=blocks_state,
        kind=kind,
    )


async def answer_clarification(
    session: AsyncSession,
    *,
    clarification_id: str,
    answer: str,
    answered_by: Literal["user", "bifrost_research", "bifrost_extraction", "system"] = "user",
) -> dict[str, Any]:
    """User (or a pipeline) provides an answer. Flips state=answered,
    decrements the task's pending counter, returns the updated row."""
    now = datetime.now(UTC)
    row = (
        await session.execute(
            text(
                """
                UPDATE task_clarifications
                SET answer = :ans,
                    answered_by = :by,
                    answered_at = :at,
                    state = 'answered'
                WHERE id = :id AND state = 'open'
                RETURNING id, task_id, question
                """
            ),
            {"ans": answer, "by": answered_by, "at": now, "id": clarification_id},
        )
    ).first()
    if row is None:
        raise ValueError(f"Clarification {clarification_id} not found or not in 'open' state")
    await session.execute(
        text(
            """
            UPDATE tasks
            SET clarifications_pending = GREATEST(0, clarifications_pending - 1),
                updated_at = :now
            WHERE id = :id
            """
        ),
        {"now": now, "id": str(row[1])},
    )
    return {
        "id": str(row[0]),
        "task_id": str(row[1]),
        "question": row[2],
        "state": "answered",
        "answered_by": answered_by,
        "answered_at": now.isoformat(),
    }


# ── Internal helpers ──────────────────────────────────────────────────


async def _insert_ask_back(
    session: AsyncSession,
    *,
    task_id: str,
    phase_id: str | None,
    agent_kind: str,
    question: str,
    scope: dict[str, Any],
    answered_from: str,
    answer_summary: str,
    research_tokens: int,
    research_cost: Decimal,
    duration_ms: int,
    answer_confidence: Decimal | None = None,
    triggered_extraction_id: str | None = None,
) -> str:
    ab_id = str(_uuid.uuid4())
    await session.execute(
        text(
            """
            INSERT INTO ask_backs
                (id, task_id, phase_id, agent_kind, question, scope,
                 answered_from, triggered_extraction_id,
                 research_tokens, research_cost, duration_ms,
                 answer_summary, answer_confidence, created_at)
            VALUES
                (:id, :task, :phase, :kind, :q, CAST(:scope AS JSONB),
                 :from, :ex_id, :tok, :cost, :dur, :sum, :conf, :at)
            """
        ),
        {
            "id": ab_id,
            "task": task_id,
            "phase": phase_id,
            "kind": agent_kind,
            "q": question,
            "scope": json.dumps(scope),
            "from": answered_from,
            "ex_id": triggered_extraction_id,
            "tok": research_tokens,
            "cost": research_cost,
            "dur": duration_ms,
            "sum": answer_summary,
            "conf": answer_confidence,
            "at": datetime.now(UTC),
        },
    )
    return ab_id


def _graph_only_summary(gap: GapAssessment | None) -> dict[str, Any]:
    """Build a stub research payload from the gap assessment alone.

    Used when the caller didn't hand us a settings+session_factory pair
    (unit tests, smoke endpoints that disable full research).
    """
    if gap is None:
        return {"answer": "", "confidence": "uncertain", "usage": {}, "sources": []}

    lines: list[str] = []
    if gap.entity_matches:
        lines.append(
            f"Top graph matches for this question (verdict: {gap.verdict}, "
            f"score {gap.coverage_score:.2f}):"
        )
        for m in gap.entity_matches[:5]:
            lines.append(f"- {m.name} ({m.entity_type}, distance {m.distance:.2f})")
    else:
        lines.append("No matching entities in graph.")
    if gap.suggested_repos_to_extract:
        lines.append("")
        lines.append("Suggested repos to extract for better coverage:")
        for s in gap.suggested_repos_to_extract:
            lines.append(f"- {s.owner}/{s.name} — {s.reason}")
    return {
        "answer": "\n".join(lines),
        "confidence": gap.verdict,
        "usage": {"cost": 0.0, "input": 0, "output": 0},
        "sources": [
            {"id": m.id, "name": m.name, "distance": m.distance} for m in gap.entity_matches[:10]
        ],
    }


async def _run_research_pipeline(
    *,
    settings: BifrostSettings,
    session_factory: async_sessionmaker[AsyncSession],
    question: str,
    scope_repo_ids: list[str] | None,
    conversation_id: str | None,
) -> dict[str, Any]:
    """Consume run_research stream, return the final `done` payload.

    Creates a transient conversation if none was provided so the
    coordinator has the context row it needs.
    """
    from bifrost.research.coordinator import ResearchDeps, run_research

    conv_uuid = _uuid.UUID(conversation_id) if conversation_id else None
    if conv_uuid is None:
        conv_uuid = await _create_transient_conversation(
            session_factory=session_factory,
            question=question,
            scope_repo_ids=scope_repo_ids,
        )

    deps = ResearchDeps(session_factory=session_factory, settings=settings)
    final: dict[str, Any] | None = None
    try:
        async for evt in run_research(
            question=question,
            conversation_id=conv_uuid,
            deps=deps,
            explicit_scope_repo_ids=scope_repo_ids,
        ):
            if evt.get("type") == "done":
                final = evt
                break
            if evt.get("type") == "error":
                return {
                    "answer": f"Research failed: {evt.get('error')}",
                    "confidence": "uncertain",
                    "usage": {},
                    "sources": [],
                }
    except Exception as e:
        logger.exception("bifrost_research pipeline crashed")
        return {
            "answer": f"Research failed: {type(e).__name__}: {e}",
            "confidence": "uncertain",
            "usage": {},
            "sources": [],
        }

    if final is None:
        return {
            "answer": "Research finished without producing an answer.",
            "confidence": "uncertain",
            "usage": {},
            "sources": [],
        }
    return {
        "answer": final.get("answer", ""),
        "confidence": final.get("confidence"),
        "usage": final.get("usage", {}),
        "sources": final.get("sources", []),
    }


async def _create_transient_conversation(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    question: str,
    scope_repo_ids: list[str] | None,
) -> _uuid.UUID:
    """Make a throwaway conversation so run_research has context.

    Not linked to any task — purely a vehicle for the coordinator. In
    practice the caller usually passes a conversation_id.
    """
    cid = _uuid.uuid4()
    now = datetime.now(UTC)
    async with session_factory() as s:
        await s.execute(
            text(
                """
                INSERT INTO conversations
                    (id, title, scope_repo_ids, created_at, updated_at)
                VALUES
                    (:id, :title, CAST(:scope AS JSONB), :now, :now)
                """
            ),
            {
                "id": cid,
                "title": f"[bifrost_research] {question[:80]}",
                "scope": json.dumps(scope_repo_ids or []),
                "now": now,
            },
        )
        # Seed with the user question
        await s.execute(
            text(
                """
                INSERT INTO conversation_messages
                    (id, conversation_id, role, content, created_at)
                VALUES
                    (:id, :cid, 'user', :c, :now)
                """
            ),
            {
                "id": _uuid.uuid4(),
                "cid": cid,
                "c": question,
                "now": now,
            },
        )
        await s.commit()
    return cid


async def _resolve_or_register_repos(
    session: AsyncSession,
    names: list[str],
) -> dict[str, dict[str, Any]]:
    """Return {name: {id, name}} for every name that maps to a real repo.

    Looks up by exact name in ``repos`` first. If not present, tries
    the org catalog — auto-registering from ``org_repos`` on hit.
    Unknown names are silently dropped (caller inspects the returned
    list).
    """
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        row = (
            await session.execute(
                text("SELECT id, name FROM repos WHERE name = :n"),
                {"n": name},
            )
        ).first()
        if row is not None:
            out[name] = {"id": str(row[0]), "name": row[1]}
            continue

        # Try to register from catalog
        cat = (
            await session.execute(
                text(
                    """
                    SELECT id, owner, name, url, default_branch
                    FROM org_repos WHERE name = :n
                    LIMIT 1
                    """
                ),
                {"n": name},
            )
        ).first()
        if cat is None:
            continue
        new_id = str(_uuid.uuid4())
        # repos.created_at/updated_at are TZ-naive — rely on server_default=NOW().
        await session.execute(
            text(
                """
                INSERT INTO repos
                    (id, name, origin_url, default_branch)
                VALUES
                    (:id, :name, :url, :branch)
                """
            ),
            {
                "id": new_id,
                "name": cat[2],
                "url": cat[3],
                "branch": cat[4] or "main",
            },
        )
        await session.execute(
            text("UPDATE org_repos SET registered_repo_id = :rid WHERE id = :oid"),
            {"rid": new_id, "oid": cat[0]},
        )
        out[name] = {"id": new_id, "name": cat[2]}
    return out


async def _create_extraction_task(
    session: AsyncSession,
    *,
    repo_id: str,
    repo_name: str,
    mode: str,
    max_files: int | None,
    force_full: bool,
    dry_run: bool,
) -> str:
    """Insert one tasks row for extraction. Returns task id."""
    task_id = str(_uuid.uuid4())
    scope: dict[str, Any] = {
        "repo_ids": [repo_id],
        "force_full": force_full,
        "mode": mode,
        "dry_run": dry_run,
    }
    if max_files is not None:
        scope["max_files"] = int(max_files)
    # Mode shapes sensible defaults when max_files is unset.
    if "max_files" not in scope:
        scope["max_files"] = 50 if mode == "shallow" else 500

    # tasks.created_at / updated_at are TIMESTAMP WITHOUT TIME ZONE —
    # rely on their server_default=NOW() rather than sending a tz-aware
    # datetime across the wire (asyncpg rejects the mismatch).
    await session.execute(
        text(
            """
            INSERT INTO extraction_tasks
                (id, task_type, query, scope_json, budget_json, status)
            VALUES
                (:id, 'extraction', :q, CAST(:scope AS JSONB),
                 CAST('{}' AS JSONB), 'pending')
            """
        ),
        {
            "id": task_id,
            "q": f"[bifrost.ask_back] {mode} extract {repo_name}",
            "scope": json.dumps(scope),
        },
    )
    return task_id


async def _wait_for_tasks(
    session: AsyncSession,
    task_ids: list[str],
    *,
    timeout_s: int,
) -> tuple[str, dict[str, str]]:
    """Poll every 2s until every task is terminal or timeout fires."""
    terminal = {"done", "failed", "cancelled"}
    deadline = time.monotonic() + timeout_s
    statuses: dict[str, str] = {t: "pending" for t in task_ids}
    while time.monotonic() < deadline:
        rows = (
            await session.execute(
                text("SELECT id::text, status FROM extraction_tasks WHERE id = ANY(:ids)"),
                {"ids": task_ids},
            )
        ).all()
        for tid, st in rows:
            statuses[tid] = st
        if all(s in terminal for s in statuses.values()):
            break
        await asyncio.sleep(2.0)

    if all(s == "done" for s in statuses.values()):
        overall = "done"
    elif any(s == "done" for s in statuses.values()):
        overall = "partial"
    elif any(s in {"failed", "cancelled"} for s in statuses.values()):
        overall = "failed"
    else:
        overall = "running"
    return overall, statuses


def _confidence_to_decimal(conf: Any) -> Decimal | None:
    if conf is None:
        return None
    if isinstance(conf, (int, float)):
        return Decimal(str(conf))
    mapping = {
        "confirmed": Decimal("0.90"),
        "inferred": Decimal("0.65"),
        "uncertain": Decimal("0.35"),
        "adequate": Decimal("0.75"),
        "partial": Decimal("0.50"),
        "insufficient": Decimal("0.20"),
    }
    return mapping.get(str(conf).lower())


# ── spawn-modal helper: suggested AC + issue type ────────────────────


_SPAWN_SUGGEST_SYSTEM = """\
You are a deterministic suggestion engine for Bifrost's task-spawn
modal. Given a chat message that the operator wants to turn into a
task, return:

  * acceptance_criteria — 3 to 5 short, testable bullets describing
    the success conditions for the work. Each bullet is one sentence,
    starts with a verb, and is observable from outside (no
    implementation details).
  * issue_type — "Task" | "Story" | "Bug" — picked from the message's
    intent (a "Bug" if the message describes broken behavior; a
    "Story" if it's user-facing functionality; otherwise "Task").

Return ONLY the schema JSON. No prose.
"""


_SPAWN_SUGGEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["acceptance_criteria", "issue_type"],
    "properties": {
        "acceptance_criteria": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 6,
        },
        "issue_type": {"type": "string"},
    },
}


async def suggest_acceptance_criteria(
    intent_text: str,
) -> tuple[list[str], str]:
    """Cheap Haiku call: return (acceptance_criteria, issue_type) for
    the spawn modal. Best-effort — caller must handle the empty / fail
    case (route layer falls back to generic placeholders)."""
    from bifrost.cli.config import get_settings
    from bifrost.extraction.cli_client import ClaudeLlmClient

    settings = get_settings()
    client = ClaudeLlmClient(settings)
    completion = await client.call_with_dynamic_max_turns(
        initial_max_turns=1,
        timeout_override=30,
        system_prompt=_SPAWN_SUGGEST_SYSTEM,
        user_message=(
            "Suggest acceptance_criteria + issue_type for this chat "
            "intent. Return only the schema JSON.\n\n"
            f"intent:\n{intent_text}\n"
        ),
        model=settings.triage_model,
        json_schema=_SPAWN_SUGGEST_SCHEMA,
    )
    parsed = completion.parsed or {}
    ac_raw = parsed.get("acceptance_criteria") or []
    ac = [str(s).strip() for s in ac_raw if str(s).strip()]
    issue_type = str(parsed.get("issue_type") or "Task").strip() or "Task"
    if issue_type not in ("Task", "Story", "Bug"):
        issue_type = "Task"
    return ac, issue_type
