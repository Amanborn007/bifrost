"""Per-task budget guard.

Every task has three caps (set at create time, adjustable):

    budget_ask_backs_max      (int)  — how many times the agent is
                                       allowed to reach back into
                                       Bifrost with a research query
    budget_cost_max_usd       (num)  — total $ the task may spend on
                                       LLM + research
    budget_extractions_max    (int)  — how many repo extractions the
                                       agent may trigger

The guard is consulted *before* any expensive operation. If a cap is
hit, `check()` raises `BudgetExceeded` carrying a human-readable reason
plus the specific cap that was exceeded. The caller (usually the tools
module) then records an `ask_backs` row with `answered_from =
'budget_exceeded'` so the audit log captures the throttle event.

The current usage is computed by SUM'ing `ask_backs.research_cost`,
counting `ask_backs` rows, and counting `extraction_triggers` rows for
the task.  This keeps the task row itself free of denormalized
counters (which would need triggers to stay consistent) and makes the
numbers self-evidently correct.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class Budget:
    ask_backs_max: int
    cost_max_usd: Decimal
    extractions_max: int


@dataclass(frozen=True)
class Usage:
    ask_backs_used: int
    cost_used_usd: Decimal
    extractions_used: int


@dataclass(frozen=True)
class BudgetSnapshot:
    budget: Budget
    usage: Usage

    @property
    def ask_backs_remaining(self) -> int:
        return max(0, self.budget.ask_backs_max - self.usage.ask_backs_used)

    @property
    def cost_remaining_usd(self) -> Decimal:
        return max(Decimal(0), self.budget.cost_max_usd - self.usage.cost_used_usd)

    @property
    def extractions_remaining(self) -> int:
        return max(0, self.budget.extractions_max - self.usage.extractions_used)

    def to_dict(self) -> dict:
        return {
            "budget": {
                "ask_backs_max": self.budget.ask_backs_max,
                "cost_max_usd": float(self.budget.cost_max_usd),
                "extractions_max": self.budget.extractions_max,
            },
            "usage": {
                "ask_backs_used": self.usage.ask_backs_used,
                "cost_used_usd": float(self.usage.cost_used_usd),
                "extractions_used": self.usage.extractions_used,
            },
            "remaining": {
                "ask_backs": self.ask_backs_remaining,
                "cost_usd": float(self.cost_remaining_usd),
                "extractions": self.extractions_remaining,
            },
        }


class BudgetExceeded(Exception):
    """Raised when a prospective op would push the task over a cap."""

    def __init__(self, cap: str, limit: float | int, used: float | int) -> None:
        self.cap = cap  # "ask_backs" | "cost_usd" | "extractions"
        self.limit = limit
        self.used = used
        super().__init__(
            f"Task budget exceeded: {cap} cap {limit} reached "
            f"(used {used}). Raise the cap or escalate to the user."
        )


async def snapshot(session: AsyncSession, task_id: str) -> BudgetSnapshot:
    """Read current usage + caps for a task."""
    row = (
        await session.execute(
            text(
                """
                SELECT
                    b.budget_ask_backs_max,
                    b.budget_cost_max_usd,
                    b.budget_extractions_max,
                    COALESCE((
                        SELECT COUNT(*) FROM ask_backs a
                        WHERE a.task_id = b.id
                    ), 0)                                   AS ab_used,
                    COALESCE((
                        SELECT SUM(a.research_cost) FROM ask_backs a
                        WHERE a.task_id = b.id
                    ), 0)                                   AS cost_used,
                    COALESCE((
                        SELECT COUNT(*) FROM extraction_triggers e
                        WHERE e.task_id = b.id
                    ), 0)                                   AS ex_used
                FROM tasks b
                WHERE b.id = :id
                """
            ),
            {"id": task_id},
        )
    ).first()
    if row is None:
        raise ValueError(f"task {task_id} not found")

    budget = Budget(
        ask_backs_max=int(row[0]),
        cost_max_usd=Decimal(str(row[1])),
        extractions_max=int(row[2]),
    )
    usage = Usage(
        ask_backs_used=int(row[3]),
        cost_used_usd=Decimal(str(row[4])),
        extractions_used=int(row[5]),
    )
    return BudgetSnapshot(budget=budget, usage=usage)


async def check(
    session: AsyncSession,
    task_id: str,
    *,
    new_cost_usd: Decimal | float = Decimal(0),
    need_ask_back: bool = False,
    need_extraction: bool = False,
) -> BudgetSnapshot:
    """Raise BudgetExceeded if the pending op would breach any cap.

    Returns the current snapshot when the op is permitted.
    """
    snap = await snapshot(session, task_id)

    if need_ask_back and snap.usage.ask_backs_used >= snap.budget.ask_backs_max:
        raise BudgetExceeded(
            "ask_backs",
            limit=snap.budget.ask_backs_max,
            used=snap.usage.ask_backs_used,
        )

    if need_extraction and snap.usage.extractions_used >= snap.budget.extractions_max:
        raise BudgetExceeded(
            "extractions",
            limit=snap.budget.extractions_max,
            used=snap.usage.extractions_used,
        )

    prospective_cost = snap.usage.cost_used_usd + Decimal(str(new_cost_usd))
    if prospective_cost > snap.budget.cost_max_usd:
        raise BudgetExceeded(
            "cost_usd",
            limit=float(snap.budget.cost_max_usd),
            used=float(prospective_cost),
        )

    return snap
