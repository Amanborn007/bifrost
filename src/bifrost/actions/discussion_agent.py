"""Discussion agent (Layer 3) — converts a raw intent into a shared
understanding.

At the end of a successful run the task has:
    * a `context` artifact with body_md = human-readable description
      of what we're building, why, and the constraints
    * a set of ``task_assumptions`` rows, each atomic + typed with
      provenance + confidence (the belief DAG's leaves)
    * an ``open_questions`` queue (task_clarifications rows) for the
      things the agent couldn't decide on its own

The agent itself is a single Claude CLI call with a tight JSON schema.
We deliberately avoid multi-turn exchanges here — the "turn" is the
human responding to clarifications via the UI, not additional LLM
reasoning. That way the audit log is simple: every call is one row in
``ask_backs`` and (if it wrote an artifact) one row in
``task_artifacts`` with version += 1.

Assumption merging
------------------
On version 2+ we don't wipe prior assumptions. The agent receives the
previous assumption set as input and returns an updated list. We
reconcile by key:
    * key present in new + previous → upsert value, bump version
    * key present in new only → insert as active
    * key present in previous only → mark retracted via state='retracted'
      pointing retracted_by_artifact_id at the new artifact

Clarification reuse
-------------------
The agent can reference a previously answered clarification (state =
answered) in its reasoning — we feed the answered Q&A pairs back in as
"resolved decisions" on the next call. Open clarifications the agent
chose NOT to ask this round stay open (the user may still answer them
through the UI).
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
from bifrost.actions.task_lifecycle import TaskState, record_event, transition
from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.extraction.cli_client import ClaudeCliError, ClaudeLlmClient

logger = logging.getLogger(__name__)


# ── Schema fed to the LLM ────────────────────────────────────────────


_DISCUSSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["context_md", "assumptions", "open_questions", "ready_to_plan"],
    "properties": {
        "context_md": {
            "type": "string",
            "description": (
                "The shared understanding of the work, as readable "
                "markdown. Short sections (## Goal, ## Scope, "
                "## Out of scope, ## Constraints)."
            ),
        },
        "assumptions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["key", "value", "source_type", "confidence", "tier"],
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "A1, A2, A3... stable across versions",
                    },
                    "value": {"type": "string"},
                    "source_type": {
                        "type": "string",
                        "enum": [
                            "user_stated",
                            "code_verified",
                            "manifest",
                            "gradle",
                            "package_json",
                            "metadata_match",
                            "confluence",
                            "jira",
                            "github_issue",
                            "slack",
                            "fuzzy_match",
                            "llm_guess",
                            "research",
                        ],
                    },
                    "confidence": {"type": "number"},
                    "tier": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "1=code_verified, 5=llm_guess",
                    },
                    "provenance": {"type": "object"},
                },
            },
        },
        "open_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["question", "kind"],
                "properties": {
                    "question": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "need_research",
                            "need_user_input",
                            "conflict",
                            "ambiguity",
                        ],
                    },
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "value": {"type": "string"},
                                "label": {"type": "string"},
                            },
                        },
                    },
                    "blocks_state": {"type": ["string", "null"]},
                },
            },
        },
        "ready_to_plan": {
            "type": "boolean",
            "description": (
                "True when the context is complete enough that the "
                "planner could start. Usually false if there are any "
                "open_questions with blocks_state != null."
            ),
        },
    },
}


_DISCUSSION_SYSTEM_PROMPT = """\
You are Bifrost's discussion agent. Your job is to take an engineering
intent (from a Jira ticket, a user paste, or a free-form description)
and turn it into a SHARED UNDERSTANDING: a short context document, a
flat list of atomic assumptions, and a queue of open questions for
things you can't decide on your own.

HARD RULES — breaking these creates downstream bugs:

1. ASSUMPTIONS ARE ATOMIC. One fact per row. "Service is Python 3.12 and
   uses FastAPI" is TWO assumptions (A1: Python 3.12, A2: FastAPI).
2. ASSUMPTIONS HAVE PROVENANCE. ``source_type`` is mandatory:
     * user_stated   — explicit from intent / message
     * manifest / gradle / package_json — from config files
     * code_verified — a specific file:line you cite
     * llm_guess     — you inferred it (confidence ≤ 0.5, tier 5)
     * research      — came from a bifrost_research call
   Never use ``llm_guess`` for something the user actually said —
   label that ``user_stated``.
3. KEYS ARE STABLE ACROSS VERSIONS. If A1 was "service = OrderService"
   in version 1, A1 must stay about the service in version 2. Use new
   keys (A7, A8...) for new assumptions.
4. OPEN QUESTIONS ARE ASKED ONLY IF THE ANSWER BLOCKS REAL WORK.
   "What's your favorite color?" never. "Which DB do we migrate to?"
   yes. Set ``blocks_state: 'drafting_plan'`` on hard blockers.
5. CONTEXT.MD IS ≤ 60 LINES. Short, scannable. Sections:
     ## Goal
     ## Scope
     ## Out of scope
     ## Constraints
   Add ## Open questions ONLY if they are referenced in the assumptions.
6. READY_TO_PLAN = true only when (a) goal is crisp, (b) scope is
   bounded, (c) all blocking open_questions have been answered in
   prior rounds.

Assume a senior engineer will read this. Don't pad it. Don't invent
technical details you can't cite. Label guesses as ``llm_guess`` with
low confidence and turn them into open_questions when they matter.

__BIFROST_BOUNDARY_SENTINEL__

Respond with ONLY the JSON object the schema expects.
""".replace("__BIFROST_BOUNDARY_SENTINEL__", BOUNDARY_SENTENCE)


# ── Result types ────────────────────────────────────────────────────


@dataclass
class DiscussionResult:
    task_id: str
    phase_id: str
    artifact_id: str
    artifact_version: int
    ready_to_plan: bool
    context_md: str
    assumptions_written: int
    assumptions_retracted: int
    clarifications_opened: int
    duration_ms: int
    cost_usd: float
    ask_back_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Entry point ─────────────────────────────────────────────────────


async def run_discussion(
    session: AsyncSession,
    *,
    task_id: str,
    user_message: str | None = None,
    phase_id: str | None = None,
    settings: BifrostSettings | None = None,
) -> DiscussionResult:
    """Run one discussion round. Writes artifact + assumptions + clarifications."""
    started = time.monotonic()
    settings = settings or get_settings()

    # 1. Load task
    task = (
        await session.execute(
            text(
                """
                SELECT id, title, intent_text, acceptance_criteria, state
                FROM tasks WHERE id = :id
                """
            ),
            {"id": task_id},
        )
    ).first()
    if task is None:
        raise ValueError(f"Task {task_id} not found")

    # 2. Ensure a phase exists
    if phase_id is None:
        phase_id = await _ensure_main_phase(session, task_id)

    # 3. Load prior context + assumptions + answered clarifications
    prior_artifact = await _load_prior_context(session, phase_id)
    prior_version = prior_artifact["version"] if prior_artifact else 0
    prior_assumptions = (
        await _load_assumptions(session, prior_artifact["id"]) if prior_artifact else []
    )
    answered_clars = await _load_answered_clarifications(session, task_id)

    # 4. Build user prompt
    user_prompt = _build_user_prompt(
        title=task[1],
        intent_text=task[2],
        acceptance_criteria=list(task[3] or []),
        prior_context_md=prior_artifact["body_md"] if prior_artifact else None,
        prior_assumptions=prior_assumptions,
        answered_clarifications=answered_clars,
        user_message=user_message,
    )

    # 5. Call Claude
    client = ClaudeLlmClient(settings)
    completion_cost = 0.0
    try:
        completion = await client.call_with_json_schema(
            system_prompt=_DISCUSSION_SYSTEM_PROMPT,
            user_message=user_prompt,
            model=settings.agent_model,  # Sonnet
            json_schema=_DISCUSSION_SCHEMA,
            max_turns=1,
            timeout_override=90,
        )
    except ClaudeCliError as exc:
        logger.exception("discussion agent LLM call failed")
        # Record a budget-visible ask_back so the failure shows up in UI
        ab_id = await _tools._insert_ask_back(
            session,
            task_id=task_id,
            phase_id=phase_id,
            agent_kind="discuss",
            question="discussion agent invocation",
            scope={"user_message": user_message or ""},
            answered_from="research_pipeline",
            answer_summary=f"LLM call failed: {exc}",
            research_tokens=0,
            research_cost=Decimal(0),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise
    data = completion.parsed or {}
    usage = completion.usage
    completion_cost = float(getattr(usage, "total_cost_usd", 0.0) or 0.0)

    # 6. Write the new artifact (version bump)
    new_version = prior_version + 1
    artifact_id = await _insert_artifact(
        session,
        phase_id=phase_id,
        version=new_version,
        body_md=data.get("context_md", ""),
        parsed={
            "ready_to_plan": bool(data.get("ready_to_plan", False)),
            "assumption_count": len(data.get("assumptions", [])),
            "open_question_count": len(data.get("open_questions", [])),
        },
    )

    # 7. Reconcile assumptions — active vs retracted
    new_assumptions = data.get("assumptions") or []
    written, retracted, retracted_ids = await _reconcile_assumptions(
        session,
        artifact_id=artifact_id,
        prior_assumptions=prior_assumptions,
        new_assumptions=new_assumptions,
    )

    # 7b. Correction cascade (Layer 5) — if assumptions were retracted,
    #     walk the belief DAG and invalidate anything downstream.
    cascade_summary: dict[str, Any] | None = None
    if retracted_ids:
        from bifrost.actions.correction_cascade import cascade_retractions

        cascade = await cascade_retractions(
            session,
            task_id=task_id,
            retracted_assumption_ids=retracted_ids,
            actor="agent:discuss",
        )
        cascade_summary = cascade.to_dict()

    # 8. Open clarifications for each new question
    opened = 0
    for q in data.get("open_questions") or []:
        await _tools.bifrost_ask_user(
            session,
            task_id=task_id,
            question=q.get("question", "").strip(),
            options=q.get("options") or [],
            kind=q.get("kind", "need_user_input"),  # type: ignore[arg-type]
            blocks_state=q.get("blocks_state"),
            phase_id=phase_id,
            origin_artifact_id=artifact_id,
            source="agent",
        )
        opened += 1

    # 9. Lifecycle advance: draft_intent → discussing
    current = TaskState(task[4])
    if current == TaskState.DRAFT_INTENT:
        try:
            await transition(
                session,
                task_id=task_id,
                to_state=TaskState.DISCUSSING,
                actor="agent:discuss",
                reason="discussion agent produced initial context.md",
            )
        except Exception:
            logger.exception("could not advance task to DISCUSSING")

    # 10. Record an ask_back row so the operation is visible in audit
    ab_id = await _tools._insert_ask_back(
        session,
        task_id=task_id,
        phase_id=phase_id,
        agent_kind="discuss",
        question="(discussion round)",
        scope={
            "user_message": user_message or "",
            "prior_version": prior_version,
            "new_version": new_version,
        },
        answered_from="research_pipeline",
        answer_summary=(data.get("context_md") or "")[:800],
        research_tokens=int(getattr(usage, "input_tokens", 0) + getattr(usage, "output_tokens", 0)),
        research_cost=Decimal(str(completion_cost)),
        duration_ms=int((time.monotonic() - started) * 1000),
        answer_confidence=Decimal("0.80") if data.get("ready_to_plan") else Decimal("0.55"),
    )

    # 11. Log event
    await record_event(
        session,
        task_id=task_id,
        actor="agent:discuss",
        action="discussion_round",
        payload={
            "artifact_id": artifact_id,
            "version": new_version,
            "assumptions_written": written,
            "assumptions_retracted": retracted,
            "clarifications_opened": opened,
            "ready_to_plan": bool(data.get("ready_to_plan")),
            "cost_usd": completion_cost,
            "cascade": cascade_summary,
        },
    )

    return DiscussionResult(
        task_id=task_id,
        phase_id=phase_id,
        artifact_id=artifact_id,
        artifact_version=new_version,
        ready_to_plan=bool(data.get("ready_to_plan", False)),
        context_md=data.get("context_md", ""),
        assumptions_written=written,
        assumptions_retracted=retracted,
        clarifications_opened=opened,
        duration_ms=int((time.monotonic() - started) * 1000),
        cost_usd=completion_cost,
        ask_back_id=ab_id,
    )


# ── DB helpers ──────────────────────────────────────────────────────


async def _ensure_main_phase(session: AsyncSession, task_id: str) -> str:
    """Return an existing phase id or create phase 0 'Main'."""
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
        text("UPDATE tasks SET phases_total = 1, updated_at = :now WHERE id = :id"),
        {"now": datetime.now(UTC), "id": task_id},
    )
    return phase_id


async def _load_prior_context(
    session: AsyncSession,
    phase_id: str,
) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                """
                SELECT id, version, body_md, parsed
                FROM task_artifacts
                WHERE phase_id = :p AND kind = 'context'
                ORDER BY version DESC
                LIMIT 1
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
        "parsed": row[3] or {},
    }


async def _load_assumptions(
    session: AsyncSession,
    artifact_id: str,
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT key, value, source_type, confidence, tier, provenance,
                       state
                FROM task_assumptions
                WHERE artifact_id = :a
                ORDER BY key
                """
            ),
            {"a": artifact_id},
        )
    ).all()
    return [
        {
            "key": r[0],
            "value": r[1],
            "source_type": r[2],
            "confidence": float(r[3]),
            "tier": int(r[4]),
            "provenance": r[5] or {},
            "state": r[6],
        }
        for r in rows
    ]


async def _load_answered_clarifications(
    session: AsyncSession,
    task_id: str,
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT question, answer, answered_by
                FROM task_clarifications
                WHERE task_id = :b AND state = 'answered'
                ORDER BY answered_at ASC
                LIMIT 50
                """
            ),
            {"b": task_id},
        )
    ).all()
    return [{"question": r[0], "answer": r[1], "answered_by": r[2]} for r in rows]


async def _insert_artifact(
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
                (:id, :p, 'context', :v, :body, 'draft',
                 CAST(:parsed AS JSONB), 'discussion_agent')
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


async def _reconcile_assumptions(
    session: AsyncSession,
    *,
    artifact_id: str,
    prior_assumptions: list[dict[str, Any]],
    new_assumptions: list[dict[str, Any]],
) -> tuple[int, int, list[str]]:
    """Write new assumptions; mark absent old ones retracted.

    Returns (rows_written, rows_retracted, retracted_assumption_ids).
    The id list feeds the correction cascade (Layer 5).
    """
    active_prior = {a["key"]: a for a in prior_assumptions if a.get("state") == "active"}
    seen_keys: set[str] = set()
    written = 0
    for a in new_assumptions:
        key = str(a.get("key") or "").strip()
        if not key:
            continue
        seen_keys.add(key)
        await session.execute(
            text(
                """
                INSERT INTO task_assumptions
                    (id, artifact_id, key, value, source_type, confidence,
                     tier, provenance, state)
                VALUES
                    (:id, :art, :key, :val, :src, :conf, :tier,
                     CAST(:prov AS JSONB), 'active')
                """
            ),
            {
                "id": str(_uuid.uuid4()),
                "art": artifact_id,
                "key": key[:32],
                "val": str(a.get("value", ""))[:2000],
                "src": str(a.get("source_type", "llm_guess")),
                "conf": Decimal(str(a.get("confidence", 0.5))),
                "tier": int(a.get("tier", 5)),
                "prov": json.dumps(a.get("provenance") or {}),
            },
        )
        written += 1

    # Retract prior keys not present in the new set. RETURNING gives us
    # the UUIDs we need to drive the cascade.
    retracted = 0
    retracted_ids: list[str] = []
    for key in active_prior.keys() - seen_keys:
        rows = (
            await session.execute(
                text(
                    """
                    UPDATE task_assumptions
                    SET state = 'retracted',
                        retracted_by_artifact_id = :new_art,
                        retracted_at = :now
                    WHERE key = :k AND state = 'active'
                      AND artifact_id IN (
                          SELECT id FROM task_artifacts
                          WHERE phase_id = (
                              SELECT phase_id FROM task_artifacts WHERE id = :new_art
                          )
                      )
                    RETURNING id::text
                    """
                ),
                {
                    "k": key,
                    "new_art": artifact_id,
                    "now": datetime.now(UTC),
                },
            )
        ).all()
        retracted += len(rows)
        retracted_ids.extend(r[0] for r in rows)

    return written, retracted, retracted_ids


def _build_user_prompt(
    *,
    title: str,
    intent_text: str,
    acceptance_criteria: list[str],
    prior_context_md: str | None,
    prior_assumptions: list[dict[str, Any]],
    answered_clarifications: list[dict[str, Any]],
    user_message: str | None,
) -> str:
    # Every variable below is attacker-controlled (intent_text,
    # acceptance_criteria, user_message) or LLM-generated from upstream
    # attacker input (prior_context_md). Wrap each in a tagged boundary
    # — see _DISCUSSION_SYSTEM_PROMPT for the model-side instruction.
    parts: list[str] = []
    parts.append(f"Task title: {title}")
    parts.append("")
    parts.append("Original intent:")
    parts.append(wrap_untrusted(intent_text or "(none)", "user_intent"))
    if acceptance_criteria:
        parts.append("")
        parts.append("Acceptance criteria:")
        ac_body = "\n".join(f"- {c}" for c in acceptance_criteria)
        parts.append(wrap_untrusted(ac_body, "acceptance_criteria"))
    if prior_context_md:
        parts.append("")
        parts.append("Prior context (version will bump after this round):")
        parts.append(wrap_untrusted(prior_context_md, "prior_context"))
    if prior_assumptions:
        parts.append("")
        parts.append("Prior assumptions (update or retain keys):")
        for a in prior_assumptions:
            if a.get("state") != "active":
                continue
            parts.append(
                f"- {a['key']}: {a['value']} "
                f"[source={a['source_type']} conf={a['confidence']:.2f} "
                f"tier={a['tier']}]"
            )
    if answered_clarifications:
        parts.append("")
        parts.append("Resolved decisions (treat as user_stated going forward):")
        for c in answered_clarifications:
            parts.append(f"- Q: {c['question']}")
            parts.append(f"  A: {c['answer']} ({c['answered_by']})")
    if user_message:
        parts.append("")
        parts.append("New user message for this round:")
        parts.append(wrap_untrusted(user_message, "user_message"))
    parts.append("")
    parts.append("Produce the shared-understanding JSON per the schema.")
    return "\n".join(parts)
