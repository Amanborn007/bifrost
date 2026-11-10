"""AgentRuntime ABC — the Track B seam.

Track A implements these directly; Track B would delegate to Managed Agents.
This ABC contains no business logic and no Track A specifics.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class AgentStatus(StrEnum):
    """Agent lifecycle states."""

    IDLE = "idle"
    RUNNING = "running"
    CHECKPOINTED = "checkpointed"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class AgentState:
    """Serializable snapshot of an agent's current state."""

    agent_id: str
    status: AgentStatus = AgentStatus.IDLE
    last_checkpoint_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskResult:
    """Outcome of an agent run.

    Contains the answer (if any), confidence, evidence references,
    and any structured data the agent produced.
    """

    agent_id: str
    task_id: str
    success: bool
    answer: str | None = None
    confidence_level: str = "uncertain"
    source_urns: list[str] = field(default_factory=list)
    reasoning: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class AgentTask:
    """Input to an agent run."""

    task_id: str
    task_type: str  # "question", "extraction", "analysis"
    payload: dict[str, Any] = field(default_factory=dict)


class AgentRuntime(ABC):
    """Abstract base for all Bifrost agents — the Track A↔B seam.

    Contract decision: the canonical Track-A contract
    is the **request/response** ``run(task) -> TaskResult``, NOT the
    streaming ``execute(task) -> AsyncIterator[AgentEvent]`` sketched in
    TRD §2.8. Rationale:

      * ``run()`` is what every agent (RepoAgent, TaskCoordinator,
        ExtractionAgent) actually implements and what the orchestrator and
        ``POST /api/v1/ask`` call. It is real and tested.
      * Live progress streaming (the 6 SSE event types) is produced at the
        **worker / API layer** (``extraction/worker.py``,
        ``extraction/query_worker.py``, ``api/sse.py``), not per-agent. An
        agent-level event stream would duplicate that with no consumer.
      * On Track B, Anthropic Managed Agents stream natively; the adapter
        there will bridge their event stream onto our SSE bus. Until
        Research Preview access lands there is nothing to stream *to*.

    So the per-agent contract is: ``run`` / ``checkpoint`` / ``get_state``
    (lifecycle) + ``get_definition_id`` (identity). ``get_definition_id``
    is the stable *template* id (e.g. ``"repo-agent"``) used to map an
    agent onto a Track-B Managed-Agent definition; ``AgentState.agent_id``
    is the per-instance id.

    Subclasses must implement:
        run()               — execute a task and return a result
        checkpoint()        — save current state for resume
        get_state()         — return current agent state
        get_definition_id() — stable agent-template id (Track-B mapping)
    """

    @abstractmethod
    async def run(self, task: AgentTask) -> TaskResult:
        """Execute a task and return the result."""
        ...

    @abstractmethod
    async def checkpoint(self) -> AgentState:
        """Save current state. Returns the persisted state snapshot."""
        ...

    @abstractmethod
    async def get_state(self) -> AgentState:
        """Return current agent state without persisting."""
        ...

    @abstractmethod
    def get_definition_id(self) -> str:
        """Return the stable agent-template id (e.g. ``"repo-agent"``).

        Track A uses it for logging/telemetry; Track B maps it onto a
        Managed-Agent *definition*. Distinct from the per-instance
        ``AgentState.agent_id``."""
        ...
