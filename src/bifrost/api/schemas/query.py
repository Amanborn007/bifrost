"""Knowledge query schemas per TRD §4.3 (task submission)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class SourceCitation(BaseModel):
    urn: str
    name: str
    entity_type: str | None = None
    relevance: float | None = None


class QueryRequest(BaseModel):
    question: str
    repo_ids: list[str] | None = None
    domain_id: str | None = None


class QueryAnswer(BaseModel):
    answer: str
    confidence: str
    sources: list[SourceCitation]
    gaps: list[str]
    conflicts: list[dict[str, Any]]
    task_id: str
