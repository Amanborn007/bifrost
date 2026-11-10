"""SSE event schemas per TRD §4.4."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

SSEEventType = Literal[
    "task.created",
    "status.changed",
    "consultation.started",
    "consultation.result",
    "synthesis.started",
    "task.delivered",
]


class TaskCreatedEvent(BaseModel):
    task_id: str
    status: Literal["received"] = "received"


class StatusChangedEvent(BaseModel):
    task_id: str
    old_status: str
    new_status: str


class ConsultationStartedEvent(BaseModel):
    task_id: str
    agent_id: str
    repo_id: str


class ConsultationResultEvent(BaseModel):
    task_id: str
    agent_id: str
    confidence: str
    gap_count: int


class SynthesisStartedEvent(BaseModel):
    task_id: str
    consultation_count: int


class TaskDeliveredEvent(BaseModel):
    task_id: str
    result: dict[str, Any]
