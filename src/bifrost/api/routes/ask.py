"""POST /api/v1/ask -- TaskCoordinator-driven question answering.

S9 (orchestration.md) describes a coordinator/skeptic pipeline that
decomposes a question, fans out to RepoAgents, synthesizes, and runs an
adversarial skeptic before returning. The implementation in
agents/coordinator.py + orchestration/skeptic.py is real and unit-tested
-- but until this route shipped, the only callers were tests. Production
POST /tasks goes through the SDLC coding-task lifecycle instead, so the
skeptic was effectively dead code on the live API.

This route gives the orchestrated pipeline a public surface. The CLI
`bifrost ask` and the spec'd e2e flow can now actually exercise the
coordinator + skeptic against the real entity store.

The skeptic only runs for task_types in DEFAULT_SKEPTIC_TASK_TYPES
({assess_impact, plan, trace_flow}) AND when synthesis confidence is
below the configured threshold. For plain `question` asks, the
coordinator still runs (decompose + dispatch + synthesize) but the
skeptic stage is skipped, matching S9 spec.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from bifrost.agents.runtime import AgentTask
from bifrost.api.deps import Repo, Settings
from bifrost.api.schemas.envelope import BifrostEnvelope, EnvelopeMeta
from bifrost.boot import create_agent_infrastructure
from bifrost.orchestration.budget import BudgetExceededError, SessionPool
from bifrost.orchestration.task import QueryType  # noqa: F401 -- referenced in docstring

router = APIRouter(prefix="/ask", tags=["ask"])


TaskTypeLiteral = Literal[
    "question",
    "trace_flow",
    "assess_impact",
    "plan",
    "compare",
    "generate_doc",
    "refresh",
]


# Confidence gate (S9). Low→high ranking mirrors orchestration.skeptic's
# _CONFIDENCE_ORDER so the gate and the skeptic agree on what "lower" means.
_CONFIDENCE_RANK: dict[str, int] = {
    "unknown": 0,
    "conflicting": 1,
    "uncertain": 2,
    "stale": 3,
    "inferred": 4,
    "confirmed": 5,
}
# Answers whose confidence rank is at/below this floor are gated.
_DEFAULT_GATE_FLOOR = "uncertain"


def _evaluate_gate(confidence: str, floor: str | None) -> tuple[bool, str | None]:
    """Return ``(gated, reason)``. Gated when the answer's confidence rank
    is at or below the floor rank — i.e. an ``unknown``/``conflicting``/
    ``uncertain`` answer is flagged needs-review rather than returned as
    authoritative. ``floor`` defaults to ``_DEFAULT_GATE_FLOOR``."""
    floor_name = (floor or _DEFAULT_GATE_FLOOR).lower()
    conf_rank = _CONFIDENCE_RANK.get((confidence or "").lower(), 0)
    floor_rank = _CONFIDENCE_RANK.get(floor_name, _CONFIDENCE_RANK[_DEFAULT_GATE_FLOOR])
    if conf_rank <= floor_rank:
        return True, (
            f"answer confidence '{confidence}' is at or below the "
            f"'{floor_name}' gate floor; treat as needs-review, "
            "not authoritative"
        )
    return False, None


class AskRequest(BaseModel):
    """Body of POST /api/v1/ask."""

    question: str = Field(..., min_length=1)
    task_type: TaskTypeLiteral = "question"
    # Optional scope hint. The coordinator currently uses an LLM-driven
    # decompose pass against all known repos, so this is informational
    # for now (passed through to AgentTask.payload for future wiring).
    repo_ids: list[str] = Field(default_factory=list)
    # Optional confidence-gate override. When the synthesized answer's
    # confidence is at/below this floor it is flagged ``gated`` (needs
    # review) instead of returned as authoritative. Defaults to
    # ``_DEFAULT_GATE_FLOOR`` when omitted.
    min_confidence: str | None = None


class AskAnswer(BaseModel):
    """Response body of POST /api/v1/ask -- the synthesized + skeptic-reviewed answer."""

    task_id: str
    answer: str
    confidence: str
    source_urns: list[str]
    gaps: list[str]
    conflicts: list[dict[str, Any]]
    agent_count: int
    total_cost_usd: float
    skeptic_ran: bool
    skeptic_findings: int
    # Confidence gate (S9): True when confidence is at/below the floor, so
    # the UI/client can flag the answer as needs-review rather than trust it.
    gated: bool = False
    gate_reason: str | None = None
    # Coverage info from S8 graceful-degradation path. coverage_level
    # is "full" when every relevant repo was consulted, "partial" when
    # the session pool capped concurrency, "none" when capacity was so
    # exhausted no agent could run.
    coverage_level: str = "full"
    consulted_repos: list[str] = Field(default_factory=list)
    dropped_repos: list[str] = Field(default_factory=list)


@router.post(
    "",
    response_model=BifrostEnvelope[AskAnswer],
    status_code=status.HTTP_200_OK,
)
async def ask(
    body: AskRequest,
    repo: Repo,
    settings: Settings,
) -> BifrostEnvelope[AskAnswer]:
    """Ask a question through the orchestrated TaskCoordinator pipeline.

    Pipeline (per S9 spec):
      1. LLM decompose: which repos are relevant?
      2. Dispatch sub-tasks to each repo's RepoAgent in parallel
      3. Synthesize: dedup, min-confidence aggregate, conflict-detect
      4. (trace/plan/impact only) Adversarial skeptic: fresh-context
         LLM review, may downgrade confidence and append "Skeptic: ..."
         findings to the gaps list
      5. Return unified result
    """
    task_id = uuid.uuid4().hex
    started = time.perf_counter()
    # EntityRepository wraps an AsyncSession; the coordinator needs both.
    # repo._session is the same session that backs the EntityRepository
    # and is used by adjacent modules (e.g. handover_generator.py).
    session = repo._session  # noqa: SLF001 -- intentional: shared session for the task

    # Composition root: build the coordinator through the track-aware
    # factory rather than hand-wiring ClaudeLlmClient + TaskCoordinator
    # here. SessionPool cap of 10 throttles concurrent agents inside this
    # one task (not per-tenant); beyond it the coordinator's
    # graceful-degradation path (S8 §8.5) reports coverage_level: partial.
    infra = create_agent_infrastructure(settings)
    coordinator = infra.make_coordinator(
        session,
        repo,
        pool=SessionPool(max_concurrent=10),
    )

    agent_task = AgentTask(
        task_id=task_id,
        task_type=body.task_type,
        payload={"question": body.question, "repo_ids": body.repo_ids},
    )

    result = await coordinator.run(agent_task)

    if not result.success and (result.data or {}).get("error_code") == BudgetExceededError.code:
        raise BudgetExceededError(result.errors[0], details={"task_id": task_id})
    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"task_id": task_id, "errors": result.errors},
        )

    data = result.data or {}
    gaps: list[str] = list(data.get("gaps") or [])
    conflicts: list[dict[str, Any]] = list(data.get("conflicts") or [])
    agent_results = data.get("agent_results") or []
    total_cost = float(data.get("total_cost", 0.0) or 0.0)

    # Skeptic signal: count gaps tagged "Skeptic:" -- this is the
    # current marker apply_skeptic_result() emits when the skeptic
    # found issues. A clean pass (skeptic ran, no findings) is not
    # currently distinguishable in the result; flag .skeptic_ran on
    # the conservative "may have run" criterion (task_type in the
    # default skeptic set), and report skeptic_findings = #tagged gaps.
    skeptic_findings = sum(1 for g in gaps if g.startswith("Skeptic:"))
    skeptic_ran = body.task_type in {"trace_flow", "assess_impact", "plan"}

    duration_ms = (time.perf_counter() - started) * 1000.0
    coverage_level = str(data.get("coverage_level", "full"))
    consulted_repos = list(data.get("consulted_repos") or [])

    # Confidence gate (S9): flag low-confidence answers as needs-review so
    # they are not presented as equal to a confirmed answer. The skeptic
    # has already (potentially) downgraded result.confidence_level upstream.
    gated, gate_reason = _evaluate_gate(result.confidence_level, body.min_confidence)

    evidence_summary = (
        f"Consulted {len(consulted_repos)} repo(s) "
        f"({coverage_level} coverage). "
        f"Skeptic {'ran' if skeptic_ran else 'skipped'}; "
        f"{skeptic_findings} finding(s)." + (" GATED: needs review." if gated else "")
    )

    return BifrostEnvelope(
        data=AskAnswer(
            task_id=task_id,
            answer=result.answer or "",
            confidence=result.confidence_level,
            source_urns=list(result.source_urns),
            gaps=gaps,
            conflicts=conflicts,
            agent_count=len(agent_results),
            total_cost_usd=total_cost,
            skeptic_ran=skeptic_ran,
            skeptic_findings=skeptic_findings,
            gated=gated,
            gate_reason=gate_reason,
            coverage_level=coverage_level,
            consulted_repos=consulted_repos,
            dropped_repos=list(data.get("dropped_repos") or []),
        ),
        # S10 trust-contract fields. Surfacing these in `meta` lets
        # spec-following clients read them in one place rather than
        # hunting through the route-specific data payload.
        meta=EnvelopeMeta(
            task_id=task_id,
            duration_ms=round(duration_ms, 2),
            confidence=result.confidence_level,
            evidence_summary=evidence_summary,
            gaps=gaps if gaps else None,
            conflicts=conflicts if conflicts else None,
            cache_status="live",
            data_source="orchestrated-coordinator",
            gated=gated,
            gate_reason=gate_reason,
        ),
    )
