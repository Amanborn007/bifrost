"""TaskCoordinator — meta-agent that decomposes, dispatches, and synthesizes.

Implements AgentRuntime ABC. Receives a question, uses an LLM call to determine
which repos are relevant, dispatches sub-tasks to RepoAgents in parallel,
runs the adversarial skeptic on low-confidence results, and synthesizes a
unified answer.

Per spec S8 §5.3: TaskCoordinator replaces DomainAgent + PlatformAgent.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bifrost.agents.runtime import (
    AgentRuntime,
    AgentState,
    AgentStatus,
    AgentTask,
    TaskResult,
)
from bifrost.core.enums import ConfidenceLevel
from bifrost.extraction.cli_client import ClaudeLlmClient
from bifrost.orchestration.budget import (
    BudgetedLlmClient,
    BudgetExceededError,
    BudgetTracker,
    SessionPool,
    SessionPoolExhaustedError,
)
from bifrost.orchestration.dispatcher import Dispatcher
from bifrost.orchestration.skeptic import (
    SkepticConfig,
    apply_skeptic_result,
    challenge,
    should_challenge,
)
from bifrost.orchestration.synthesis import synthesize
from bifrost.orchestration.task import (
    OrchestrationQuery,
    QueryRunState,
    QueryType,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from bifrost.storage.repository import EntityRepository

logger = logging.getLogger(__name__)

DECOMPOSITION_SYSTEM_PROMPT = """\
You are a task decomposition agent for an engineering knowledge platform.
Given a user question and a list of available repositories, determine which
repositories are relevant to answering the question.

Use the select_repos tool to indicate which repos should be consulted.
"""

SELECT_REPOS_TOOL: dict[str, Any] = {
    "name": "select_repos",
    "description": "Select repositories relevant to the question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "repo_urns": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "URNs of repos to consult. Include all that may have relevant knowledge."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "Task explanation of why these repos were selected.",
            },
        },
        "required": ["repo_urns", "reasoning"],
    },
}


class TaskCoordinator(AgentRuntime):
    """Meta-agent: decomposes queries, dispatches to RepoAgents, synthesizes answers.

    The coordinator does NOT maintain persistent state. It operates on task context
    assembled at invocation time (per spec S8 §5.3: "Knowledge owned: None persistent").
    """

    def __init__(
        self,
        client: ClaudeLlmClient,
        session: AsyncSession,
        entity_repo: EntityRepository,
        *,
        model: str = "claude-sonnet-4-6",
        pool: SessionPool | None = None,
        budget: BudgetTracker | None = None,
        skeptic_config: SkepticConfig | None = None,
    ) -> None:
        self._agent_id = f"coordinator-{uuid.uuid4().hex[:8]}"
        self._budget = budget or BudgetTracker()
        self._client = client
        self._billed_client = BudgetedLlmClient(client, self._budget, agent_id=self._agent_id)
        self._session = session
        self._entity_repo = entity_repo
        self._model = model
        self._pool = pool or SessionPool()
        self._skeptic_config = skeptic_config or SkepticConfig()
        self._dispatcher = Dispatcher(self._billed_client, session, model=model)
        self._state = AgentState(agent_id=self._agent_id)

    async def run(self, task: AgentTask) -> TaskResult:
        """Execute a coordinated multi-repo task.

        1. Decompose query into relevant repos (LLM-driven)
        2. Dispatch sub-tasks to RepoAgents in parallel
        3. Synthesize results
        4. Run adversarial skeptic if needed
        5. Return unified TaskResult
        """
        self._state.status = AgentStatus.RUNNING
        question = task.payload.get("question", "")
        task_type = (
            QueryType(task.task_type)
            if task.task_type in QueryType.__members__.values()
            else QueryType.QUESTION
        )

        if not question:
            self._state.status = AgentStatus.FAILED
            return TaskResult(
                agent_id=self._agent_id,
                task_id=task.task_id,
                success=False,
                errors=["No question provided in task payload"],
            )

        try:
            # Step 1: Determine scope — which repos to consult
            repo_map = await self._decompose(question)
            self._budget.ensure_within_budget()
            if not repo_map:
                self._state.status = AgentStatus.COMPLETED
                return TaskResult(
                    agent_id=self._agent_id,
                    task_id=task.task_id,
                    success=True,
                    answer="No relevant repositories found for this question.",
                    confidence_level=ConfidenceLevel.UNKNOWN,
                )

            # Step 2: Acquire pool slots — degrade gracefully if exhausted.
            # S8 spec section 8.5 calls for queued-then-degrade to
            # ``status: 'degraded', coverage_level: 'partial'`` when the
            # pool is at capacity. Pre-fix the pool raised, the
            # coordinator caught the exception generically, and the
            # task failed. Instead, acquire what we can and proceed
            # with the subset; record the dropped repos as a known
            # coverage gap.
            acquired_urns: list[str] = []
            dropped_urns: list[str] = []
            for urn in repo_map:
                agent_id = f"repo-agent-{repo_map[urn].hex[:8]}"
                try:
                    self._pool.acquire(agent_id)
                    acquired_urns.append(urn)
                except SessionPoolExhaustedError:
                    dropped_urns.append(urn)

            if not acquired_urns:
                # Could not run any agent — full degradation, no answer.
                self._state.status = AgentStatus.COMPLETED
                return TaskResult(
                    agent_id=self._agent_id,
                    task_id=task.task_id,
                    success=True,
                    answer=(
                        "Session pool exhausted before any agent could run. "
                        "Try again with fewer concurrent tasks."
                    ),
                    confidence_level=ConfidenceLevel.UNKNOWN,
                    data={
                        "coverage_level": "none",
                        "dropped_repos": dropped_urns,
                    },
                )

            acquired_map = {u: repo_map[u] for u in acquired_urns}
            orchestrated = OrchestrationQuery(
                id=task.task_id,
                query=question,
                target_repos=list(acquired_map.keys()),
                task_type=task_type,
            )

            try:
                agent_results = await self._dispatcher.dispatch_parallel(
                    acquired_map,
                    orchestrated,
                    timeout=120.0,
                )
            finally:
                # Always release pool slots for whatever we acquired
                for urn in acquired_urns:
                    agent_id = f"repo-agent-{acquired_map[urn].hex[:8]}"
                    self._pool.release(agent_id)

            # Step 3: Synthesize
            synthesis = synthesize(agent_results)
            if self._budget.exceeded and not any(r.success for r in agent_results.values()):
                self._budget.ensure_within_budget()

            # Step 4: Adversarial skeptic
            if self._budget.exceeded:
                synthesis.gaps.append(
                    f"Session budget exceeded (${self._budget.total_cost:.2f}); "
                    "remaining LLM calls were skipped."
                )
            elif should_challenge(synthesis, task_type, self._skeptic_config):
                skeptic_result = await challenge(
                    self._billed_client,
                    synthesis,
                    self._skeptic_config,
                )
                synthesis = apply_skeptic_result(synthesis, skeptic_result)

            # Step 5: Build final TaskResult — walk to COMPLETED
            # dispatch_parallel may have already advanced state;
            # walk from current state to COMPLETED
            _walk_to_completed(orchestrated)
            orchestrated.result = synthesis

            # Surface coverage degradation in gaps + structured data so
            # callers (UI, CLI, the /ask response model) can render it
            # honestly instead of pretending we covered everything.
            coverage_level = "partial" if dropped_urns else "full"
            if dropped_urns:
                synthesis.gaps.append(
                    f"Coverage degraded: skipped {len(dropped_urns)} repo(s) "
                    f"due to session-pool capacity ({len(acquired_urns)} "
                    f"consulted, {len(dropped_urns)} dropped)."
                )

            self._state.status = AgentStatus.COMPLETED
            return TaskResult(
                agent_id=self._agent_id,
                task_id=task.task_id,
                success=True,
                answer=synthesis.answer,
                confidence_level=synthesis.confidence,
                source_urns=synthesis.source_urns,
                reasoning=(
                    f"Consulted {len(acquired_urns)} of {len(repo_map)} repo(s). "
                    f"Conflicts: {len(synthesis.conflicts)}. "
                    f"Gaps: {len(synthesis.gaps)}."
                ),
                data={
                    "agent_results": synthesis.agent_results,
                    "conflicts": synthesis.conflicts,
                    "gaps": synthesis.gaps,
                    "total_cost": self._budget.total_cost,
                    "coverage_level": coverage_level,
                    "consulted_repos": acquired_urns,
                    "dropped_repos": dropped_urns,
                },
            )

        except BudgetExceededError as exc:
            self._state.status = AgentStatus.FAILED
            logger.warning("TaskCoordinator stopped for task %s: %s", task.task_id, exc)
            return TaskResult(
                agent_id=self._agent_id,
                task_id=task.task_id,
                success=False,
                errors=[str(exc)],
                data={"error_code": exc.code},
            )
        except Exception as exc:
            self._state.status = AgentStatus.FAILED
            logger.exception("TaskCoordinator failed for task %s", task.task_id)
            return TaskResult(
                agent_id=self._agent_id,
                task_id=task.task_id,
                success=False,
                errors=[str(exc)],
            )

    async def checkpoint(self) -> AgentState:
        self._state.last_checkpoint_at = datetime.now(UTC)
        return self._state

    async def get_state(self) -> AgentState:
        return self._state

    def get_definition_id(self) -> str:
        return "task-coordinator"

    # ── Feedback Loop ────────────────────────────────────────────────

    async def submit_feedback(
        self,
        entity_urns: list[str],
        helpful: bool,
        correction: str | None = None,
    ) -> dict[str, str]:
        """Apply user feedback to entity confidence scores.

        Positive feedback: bump confidence toward CONFIRMED.
        Negative feedback: degrade confidence toward UNCERTAIN, flag for re-extraction.

        Persists changes via EntityRepository — not just in-memory.
        """
        updates: dict[str, str] = {}
        for urn in entity_urns:
            entity = await self._entity_repo.get_entity_by_urn(urn)
            if entity is None:
                updates[urn] = "not_found"
                continue

            current = entity.confidence_level or ConfidenceLevel.UNCERTAIN
            new_level = _bump_confidence(current) if helpful else _degrade_confidence(current)

            if new_level != current:
                # Update via direct SQL to persist — not just in-memory
                from sqlalchemy import update

                from bifrost.storage.models import Entity

                stmt = update(Entity).where(Entity.urn == urn).values(confidence_level=new_level)
                await self._session.execute(stmt)
                updates[urn] = f"{current} -> {new_level}"
            else:
                updates[urn] = f"{current} (unchanged)"

        await self._session.flush()
        logger.info(
            "Feedback applied: helpful=%s, entities=%d, updates=%s",
            helpful,
            len(entity_urns),
            updates,
        )
        return updates

    # ── Private ──────────────────────────────────────────────────────

    async def _decompose(self, question: str) -> dict[str, uuid.UUID]:
        """Use LLM to determine which repos are relevant to the question.

        Returns {repo_urn: repo_id} for repos to consult.
        """
        # Fetch all known repos
        repos = await self._entity_repo.list_repos()
        if not repos:
            return {}

        repo_list = "\n".join(f"- {r.urn}: {r.name} ({r.url or 'no URL'})" for r in repos)

        completion = await self._billed_client.call_with_json_schema(
            system_prompt=DECOMPOSITION_SYSTEM_PROMPT,
            user_message=(
                f"Question: {question}\n\n"
                f"Available repositories:\n{repo_list}\n\n"
                "Select the repositories that should be consulted."
            ),
            model=self._model,
            json_schema=SELECT_REPOS_TOOL["input_schema"],
        )
        selected_urns = completion.parsed.get("repo_urns", []) or []

        # Map URNs to IDs
        urn_to_repo = {r.urn: r for r in repos}
        result: dict[str, uuid.UUID] = {}
        for urn in selected_urns:
            repo = urn_to_repo.get(urn)
            if repo:
                result[urn] = repo.id
            else:
                logger.warning("LLM selected unknown repo URN: %s", urn)

        return result


# ── State machine helper ─────────────────────────────────────────────

# Walk through the required intermediate states to reach COMPLETED.
# dispatch_parallel normally transitions PENDING -> DISPATCHED -> RUNNING,
# but if it's been mocked or partially advanced, we fill in the gaps.
_PATH_TO_COMPLETED: list[QueryRunState] = [
    QueryRunState.DISPATCHED,
    QueryRunState.RUNNING,
    QueryRunState.COMPLETED,
]


def _walk_to_completed(task: OrchestrationQuery) -> None:
    """Advance task to COMPLETED, filling intermediate states only forward.

    State transitions are one-way (see _TRANSITIONS in task.py); the prior
    implementation iterated over every target and skipped only on exact
    match, which meant a task already at RUNNING tried to transition
    backwards to DISPATCHED on the first loop iteration and raised. Now
    we find the current state's index in the path and walk only forward
    from there.
    """
    if task.state == QueryRunState.COMPLETED:
        return
    try:
        start = _PATH_TO_COMPLETED.index(task.state) + 1
    except ValueError:
        # State is not in the path (e.g., PENDING); start from the top.
        start = 0
    for target in _PATH_TO_COMPLETED[start:]:
        if task.state == QueryRunState.COMPLETED:
            break
        task.transition(target)


# ── Confidence adjustment helpers ────────────────────────────────────

_CONFIDENCE_UP: dict[str, str] = {
    ConfidenceLevel.UNKNOWN: ConfidenceLevel.UNCERTAIN,
    ConfidenceLevel.UNCERTAIN: ConfidenceLevel.INFERRED,
    ConfidenceLevel.STALE: ConfidenceLevel.INFERRED,
    ConfidenceLevel.INFERRED: ConfidenceLevel.CONFIRMED,
    ConfidenceLevel.CONFIRMED: ConfidenceLevel.CONFIRMED,
    ConfidenceLevel.CONFLICTING: ConfidenceLevel.UNCERTAIN,
}

_CONFIDENCE_DOWN: dict[str, str] = {
    ConfidenceLevel.CONFIRMED: ConfidenceLevel.INFERRED,
    ConfidenceLevel.INFERRED: ConfidenceLevel.UNCERTAIN,
    ConfidenceLevel.UNCERTAIN: ConfidenceLevel.UNCERTAIN,
    ConfidenceLevel.STALE: ConfidenceLevel.UNCERTAIN,
    ConfidenceLevel.UNKNOWN: ConfidenceLevel.UNKNOWN,
    ConfidenceLevel.CONFLICTING: ConfidenceLevel.UNCERTAIN,
}


def _bump_confidence(current: str) -> str:
    return _CONFIDENCE_UP.get(current, current)


def _degrade_confidence(current: str) -> str:
    return _CONFIDENCE_DOWN.get(current, ConfidenceLevel.UNCERTAIN)
