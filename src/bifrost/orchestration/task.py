"""Query-run model and state machine for orchestration (read path).

Defines the in-memory lifecycle for a single multi-agent **knowledge query**:
PENDING -> DISPATCHED -> RUNNING -> COMPLETED | FAILED | TIMEOUT. State
transitions are enforced at the model level — invalid transitions raise
ValueError.

Disambiguation: this used to export ``TaskState`` / ``Task`` /
``TaskType``, which collided with the *entirely different* SDLC lifecycle in
``actions/task_lifecycle.py`` (``TaskState``: DRAFT_INTENT…PR_MERGED, persisted
to the ``tasks`` table). The two share no states, storage, or consumers. To kill
the footgun, this read-path coordinator state is now ``QueryRunState`` /
``OrchestrationQuery`` / ``QueryType``; ``actions.task_lifecycle.TaskState``
remains the single ``TaskState``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class QueryRunState(StrEnum):
    """Knowledge-query coordinator lifecycle states with enforced transitions.

    Ephemeral and in-memory — one ``OrchestrationQuery`` per ``/ask`` request.
    Not to be confused with ``actions.task_lifecycle.TaskState`` (the persisted
    SDLC coding-work lifecycle).
    """

    PENDING = "pending"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


# Valid state transitions: from_state -> set of allowed to_states
_TRANSITIONS: dict[QueryRunState, set[QueryRunState]] = {
    QueryRunState.PENDING: {QueryRunState.DISPATCHED, QueryRunState.FAILED},
    QueryRunState.DISPATCHED: {
        QueryRunState.RUNNING,
        QueryRunState.FAILED,
        QueryRunState.TIMEOUT,
    },
    QueryRunState.RUNNING: {
        QueryRunState.COMPLETED,
        QueryRunState.FAILED,
        QueryRunState.TIMEOUT,
    },
    # Terminal states — no outbound transitions
    QueryRunState.COMPLETED: set(),
    QueryRunState.FAILED: set(),
    QueryRunState.TIMEOUT: set(),
}


class QueryType(StrEnum):
    """Types of knowledge query the coordinator can process."""

    QUESTION = "question"
    TRACE_FLOW = "trace_flow"
    ASSESS_IMPACT = "assess_impact"
    PLAN = "plan"
    COMPARE = "compare"
    GENERATE_DOC = "generate_doc"
    REFRESH = "refresh"


@dataclass
class OrchestratedTaskResult:
    """Outcome of a coordinated multi-agent query.

    Distinct from agents.runtime.TaskResult which is per-agent.
    This represents the synthesized result across all consulted agents.
    """

    answer: str
    confidence: str  # ConfidenceLevel value
    source_urns: list[str] = field(default_factory=list)
    agent_results: list[dict[str, object]] = field(default_factory=list)
    conflicts: list[dict[str, object]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    total_cost: float = 0.0
    total_duration_ms: float = 0.0


@dataclass
class OrchestrationQuery:
    """A coordinated knowledge query that spans one or more repos/agents.

    State transitions are enforced — call transition() to advance state.
    """

    id: str
    query: str
    target_repos: list[str]
    task_type: QueryType = QueryType.QUESTION
    state: QueryRunState = QueryRunState.PENDING
    assigned_agents: list[str] = field(default_factory=list)
    result: OrchestratedTaskResult | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    error: str | None = None

    def transition(self, to_state: QueryRunState) -> None:
        """Advance to a new state. Raises ValueError on invalid transition."""
        allowed = _TRANSITIONS.get(self.state, set())
        if to_state not in allowed:
            allowed_str = sorted(s.value for s in allowed) if allowed else "none (terminal state)"
            msg = (
                f"Invalid state transition: "
                f"{self.state.value} -> {to_state.value}. "
                f"Allowed: {allowed_str}"
            )
            raise ValueError(msg)
        self.state = to_state
        self.updated_at = datetime.now(UTC)
