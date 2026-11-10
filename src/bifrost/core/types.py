"""Primitive types, evidence references, and metadata envelope — TRD §2.1-2.4."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, Field

from bifrost.core.enums import ConfidenceLevel

# ---- Primitive type aliases --------------------------------------------------

EntityId: TypeAlias = uuid.UUID
URN: TypeAlias = str
Timestamp: TypeAlias = datetime
KnowledgeTier: TypeAlias = Literal[0, 1, 2, 3]
Scope: TypeAlias = Literal["repo", "domain", "org"]


# ---- Evidence ----------------------------------------------------------------


class LineRange(BaseModel):
    """Inclusive line range within a source file."""

    start: int
    end: int


class EvidenceRef(BaseModel):
    """A pointer to the specific source material that supports a claim."""

    source_id: str
    adapter_id: str
    file_ref: str | None = None
    line_range: LineRange | None = None
    snippet: str | None = None
    confidence: ConfidenceLevel
    extracted_at: Timestamp


# ---- Metadata envelope -------------------------------------------------------


class FreshnessInfo(BaseModel):
    """Freshness state embedded in every MetadataEnvelope."""

    indexed_at: Timestamp
    source_revision: str | None = None
    last_checked_at: Timestamp
    lag_indicator: Literal[
        "current",
        "slightly-behind",
        "stale",
        "critically-stale",
        "unknown",
    ]


class ConfidenceInfo(BaseModel):
    """Confidence assessment embedded in every MetadataEnvelope."""

    level: ConfidenceLevel
    score: float = Field(ge=0.0, le=1.0)
    basis: str


class KnownGap(BaseModel):
    """A documented gap in knowledge coverage."""

    description: str
    severity: Literal["critical", "moderate", "minor"]
    suggested_source: str | None = None


class MetadataEnvelope(BaseModel):
    """Standard quality envelope carried by every KnowledgeArtifact."""

    created_at: Timestamp
    updated_at: Timestamp
    created_by: str
    version: int
    source_refs: list[EvidenceRef]
    freshness: FreshnessInfo
    confidence: ConfidenceInfo
    scope: Scope
    known_gaps: list[KnownGap] = Field(default_factory=list)
