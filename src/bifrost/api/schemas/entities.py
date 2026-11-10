"""Entity endpoint schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class EntitySummary(BaseModel):
    id: str
    urn: str
    name: str
    entity_type: str
    confidence_level: str
    freshness_state: str


class EntityDetail(BaseModel):
    id: str
    urn: str
    name: str
    qualified_name: str | None
    entity_type: str
    knowledge_tier: int
    metadata: dict[str, Any]
    confidence: float
    confidence_level: str
    freshness_state: str
    source_basis: Any  # JSON array in DB, typed loosely for serialization
    repo_id: str | None
    domain_id: str | None
    created_at: datetime
    updated_at: datetime


class EntitySearchResult(BaseModel):
    entity: EntitySummary
    score: float
