"""Feedback endpoint schemas per TRD §4.6."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class CorrectionEntry(BaseModel):
    entity_id: str | None = None
    original_claim: str
    corrected_claim: str
    correction_type: str


class SubmitFeedbackRequest(BaseModel):
    task_id: str
    quality_signal: Literal["helpful", "partially-helpful", "unhelpful"]
    corrections: list[CorrectionEntry] | None = None
    gap_descriptions: list[str] | None = None
    free_text: str | None = None


class SubmitFeedbackData(BaseModel):
    feedback_id: str
    corrections_created: int
    gap_reports_created: int
