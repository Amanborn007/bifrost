"""Session pool and budget management for orchestration.

SessionPool enforces max concurrent agents (default: 3 per TRD §7).
BudgetTracker aggregates cost across all agents in a session.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from bifrost.errors import BifrostError

logger = logging.getLogger(__name__)


class SessionPoolExhaustedError(Exception):
    """Raised when session pool is at capacity and cannot accept more agents."""


class BudgetExceededError(BifrostError):
    """Raised when a session's total cost exceeds the configured budget."""

    code = "budget.exceeded"
    http_status = 402


@dataclass
class SessionPoolConfig:
    """Configuration for the agent session pool."""

    max_concurrent: int = 3
    queue_size: int = 10
    timeout_per_task_s: float = 120.0


class SessionPool:
    """Tracks and limits concurrent agent usage.

    Agents must acquire() before running and release() when done.
    If the pool is at capacity, acquire() raises SessionPoolExhaustedError.
    """

    def __init__(self, max_concurrent: int = 3) -> None:
        self._max = max_concurrent
        self._active: set[str] = set()

    def acquire(self, agent_id: str) -> None:
        """Reserve a slot for an agent. Raises if at capacity."""
        if agent_id in self._active:
            return  # Already acquired, idempotent
        if len(self._active) >= self._max:
            raise SessionPoolExhaustedError(
                f"Session pool at capacity ({self._max}). "
                f"Active agents: {sorted(self._active)}. "
                f"Cannot add {agent_id}. Limit enforced per TRD §7."
            )
        self._active.add(agent_id)
        logger.debug("Pool acquired: %s (%d/%d)", agent_id, len(self._active), self._max)

    def release(self, agent_id: str) -> None:
        """Release an agent's slot back to the pool."""
        self._active.discard(agent_id)
        logger.debug("Pool released: %s (%d/%d)", agent_id, len(self._active), self._max)

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def available(self) -> int:
        return self._max - len(self._active)

    @property
    def active_agents(self) -> set[str]:
        return set(self._active)


@dataclass
class CostEntry:
    """A single cost record from an agent's LLM call."""

    agent_id: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class BudgetTracker:
    """Aggregates cost across all agents in a session.

    Tracks total cost and raises BudgetExceededError if the limit is breached.
    Cost is tracked across ALL agents, not per-agent.
    """

    def __init__(self, max_budget_usd: float = 5.0) -> None:
        self._max = max_budget_usd
        self._entries: list[CostEntry] = []
        self._total: float = 0.0

    def record(self, entry: CostEntry) -> None:
        """Record a cost entry. Raises if budget exceeded."""
        self._entries.append(entry)
        self._total += entry.cost_usd
        if self._total > self._max:
            raise BudgetExceededError(
                f"Session budget exceeded: ${self._total:.4f} > ${self._max:.4f}. "
                f"Aborting session. Total entries: {len(self._entries)}."
            )
        logger.debug(
            "Cost recorded: $%.4f from %s (total: $%.4f / $%.4f)",
            entry.cost_usd,
            entry.agent_id,
            self._total,
            self._max,
        )

    @property
    def exceeded(self) -> bool:
        return self._total > self._max

    def ensure_within_budget(self) -> None:
        """Raise before spending more once the budget is already blown."""
        if self.exceeded:
            raise BudgetExceededError(
                f"Session budget exceeded: ${self._total:.4f} > ${self._max:.4f}. "
                "No further LLM calls will run.",
                details={"total_usd": self._total, "max_usd": self._max},
            )

    @property
    def total_cost(self) -> float:
        return self._total

    @property
    def remaining(self) -> float:
        return max(0.0, self._max - self._total)

    @property
    def entries(self) -> list[CostEntry]:
        return list(self._entries)


class BudgetedLlmClient:
    """Bills every LLM call to a :class:`BudgetTracker`.

    The CLI-reported ``usage.total_cost_usd`` is the billed number (the
    extraction pipeline records the same field). A call that crosses the
    budget still returns its completion; every call after it is refused.
    """

    def __init__(self, client: Any, tracker: BudgetTracker, *, agent_id: str) -> None:
        self._client = client
        self._tracker = tracker
        self._agent_id = agent_id

    async def call_with_json_schema(self, **kwargs: Any) -> Any:
        self._tracker.ensure_within_budget()
        completion = await self._client.call_with_json_schema(**kwargs)
        usage = completion.usage
        try:
            self._tracker.record(
                CostEntry(
                    agent_id=self._agent_id,
                    model=str(kwargs.get("model", "")),
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=usage.total_cost_usd,
                )
            )
        except BudgetExceededError:
            logger.warning("session budget crossed by %s; later calls are refused", self._agent_id)
        return completion
