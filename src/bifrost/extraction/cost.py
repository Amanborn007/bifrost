"""LLM cost tracking and budget enforcement for extraction sessions.

Records per-call token counts, computes estimated cost, and aborts
when a configurable budget ceiling is exceeded.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from bifrost.storage.models import LLMCostLog

logger = logging.getLogger(__name__)


# ── Model pricing (USD per 1M tokens) ────────────────────────────────

MODEL_PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
    "claude-sonnet-4-6-20250514": {
        "input": 3.00,
        "output": 15.00,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
    "claude-sonnet-4-20250514": {
        "input": 3.00,
        "output": 15.00,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
    "claude-haiku-4-5-20251001": {
        "input": 1.00,
        "output": 5.00,
        "cache_read": 0.10,
        "cache_write": 1.25,
    },
    "claude-opus-4-7": {
        "input": 15.00,
        "output": 75.00,
        "cache_read": 1.50,
        "cache_write": 18.75,
    },
    "claude-opus-4-8": {
        "input": 15.00,
        "output": 75.00,
        "cache_read": 1.50,
        "cache_write": 18.75,
    },
}

# Fallback if model not in table — Sonnet pricing is conservative.
_DEFAULT_PRICES = {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75}


class BudgetExceededError(Exception):
    """Raised when cumulative extraction cost exceeds the configured budget."""

    def __init__(self, budget: float, spent: float, last_call_cost: float) -> None:
        self.budget = budget
        self.spent = spent
        self.last_call_cost = last_call_cost
        super().__init__(
            f"Budget exceeded: ${spent:.4f} spent (limit ${budget:.4f}), "
            f"last call cost ${last_call_cost:.4f}"
        )


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> float:
    """Compute estimated USD cost for a single LLM call."""
    prices = MODEL_PRICES.get(model, _DEFAULT_PRICES)
    cost = (
        (input_tokens / 1_000_000) * prices["input"]
        + (output_tokens / 1_000_000) * prices["output"]
        + (cache_read_tokens / 1_000_000) * prices["cache_read"]
    )
    return cost


# ── Per-call record ───────────────────────────────────────────────────


@dataclass
class CostRecord:
    """One recorded LLM API call within an extraction session."""

    model: str
    phase: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


# ── Cost tracker — accumulates per-session ────────────────────────────


@dataclass
class CostTracker:
    """Tracks cumulative cost for a single extraction session.

    Usage::

        tracker = CostTracker(task_id="ext-123", budget=0.50)
        tracker.record(model="claude-sonnet-4-6-20250514", phase="D",
                       input_tokens=5000, output_tokens=800)
        # raises BudgetExceededError if total exceeds budget
    """

    task_id: str
    repo_id: str | None = None
    budget: float | None = None  # None = no limit
    records: list[CostRecord] = field(default_factory=list)
    on_record: Callable[[CostRecord, CostTracker], None] | None = None
    # Index of the next un-persisted record. Grows as ``flush_new()``
    # writes batches to llm_cost_log during a running extraction, so the
    # /spend page updates live instead of jumping at task completion.
    _persisted_count: int = 0

    @property
    def total_cost(self) -> float:
        return sum(r.cost_usd for r in self.records)

    @property
    def total_input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.records)

    @property
    def total_output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.records)

    @property
    def total_cache_read_tokens(self) -> int:
        return sum(r.cache_read_tokens for r in self.records)

    def per_phase_cost(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in self.records:
            out[r.phase] = out.get(r.phase, 0.0) + r.cost_usd
        return out

    def per_phase_calls(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.records:
            out[r.phase] = out.get(r.phase, 0) + 1
        return out

    def record(
        self,
        *,
        model: str,
        phase: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        reported_cost_usd: float | None = None,
    ) -> CostRecord:
        """Record a single LLM call and enforce budget.

        ``reported_cost_usd`` is Claude CLI's self-reported cost for the call
        (from the ``total_cost_usd`` field of the stream-json result event).
        When provided and > 0 it is treated as authoritative — the billing
        engine and our price table can drift, and the CLI's number tracks
        Anthropic's actual meter. We fall back to the price-table estimate
        only when the CLI reports zero (e.g., dry-run mode, streaming errors).

        Returns the CostRecord. Raises BudgetExceededError if budget exceeded.
        """
        estimate = estimate_cost(model, input_tokens, output_tokens, cache_read_tokens)
        if reported_cost_usd is not None and reported_cost_usd > 0:
            cost = float(reported_cost_usd)
            source = "cli"
            # Only loud-warn on BIG divergence — minor estimates-vs-billing
            # drift is normal (cache-write vs cache-read mix isn't captured
            # by estimate_cost). CLI is authoritative anyway.
            if estimate > 0 and abs(cost - estimate) / max(cost, estimate) > 0.5:
                logger.warning(
                    "cost divergence >50%%: CLI=$%.4f estimate=$%.4f (phase=%s model=%s)",
                    cost,
                    estimate,
                    phase,
                    model,
                )
        else:
            cost = estimate
            source = "estimate"

        rec = CostRecord(
            model=model,
            phase=phase,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cost_usd=cost,
        )
        self.records.append(rec)

        logger.info(
            "LLM call: model=%s phase=%s in=%d out=%d cache_read=%d cost=$%.4f (%s) total=$%.4f",
            model,
            phase,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cost,
            source,
            self.total_cost,
        )

        if self.on_record is not None:
            try:
                self.on_record(rec, self)
            except Exception:
                logger.exception("cost on_record callback failed (non-fatal)")

        if self.budget is not None and self.total_cost > self.budget:
            raise BudgetExceededError(
                budget=self.budget,
                spent=self.total_cost,
                last_call_cost=cost,
            )

        return rec

    async def flush_new(self, session: AsyncSession) -> int:
        """Write only un-persisted records to llm_cost_log.

        Meant to be called periodically during a long extraction so the
        /spend page reflects live burn. Safe to call multiple times —
        records already written are skipped via the _persisted_count marker.
        """
        start = self._persisted_count
        new = self.records[start:]
        if not new:
            return 0
        rows = [
            LLMCostLog(
                id=uuid.uuid4(),
                task_id=self.task_id,
                repo_id=uuid.UUID(self.repo_id) if self.repo_id else None,
                phase=rec.phase,
                model=rec.model,
                input_tokens=rec.input_tokens,
                output_tokens=rec.output_tokens,
                estimated_cost_usd=rec.cost_usd,
                skipped=False,
            )
            for rec in new
        ]
        session.add_all(rows)
        await session.flush()
        self._persisted_count = len(self.records)
        return len(rows)

    async def persist(self, session: AsyncSession) -> int:
        """Write all un-persisted records to llm_cost_log (end-of-task).

        Delegates to ``flush_new`` — kept for backward compatibility.
        Returns the number of rows inserted this call.
        """
        n = await self.flush_new(session)
        logger.info(
            "Persisted %d cost records for task %s (total $%.4f)",
            n,
            self.task_id,
            self.total_cost,
        )
        return n
