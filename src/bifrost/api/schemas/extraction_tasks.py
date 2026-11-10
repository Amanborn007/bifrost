"""Task endpoint schemas per TRD §4.3."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class TaskBudget(BaseModel):
    max_llm_calls: int | None = None
    max_wall_time_ms: int | None = None


class TaskScope(BaseModel):
    repo_ids: list[str] | None = None
    domain_id: str | None = None


class SubmitTaskRequest(BaseModel):
    type: str
    query: str
    scope: TaskScope
    budget: TaskBudget | None = None
    freshness_requirement: str | None = None


class SubmitTaskData(BaseModel):
    task_id: str
    status: Literal["received"] = "received"
    stream_url: str


class TaskSummary(BaseModel):
    id: str
    type: str
    status: str
    query: str
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] | None = None  # expose result summary for richer activity views
    error_message: str | None = None


class TaskDetail(BaseModel):
    id: str
    type: str
    status: str
    query: str
    result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class CancelTaskData(BaseModel):
    id: str
    status: Literal["cancelled"] = "cancelled"


class TaskActionData(BaseModel):
    id: str
    status: str
