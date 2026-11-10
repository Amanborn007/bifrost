"""Core Pydantic entity models — TRD §2.12-2.14.

All entity types that Bifrost tracks. These are the domain models used throughout
the system. SQLAlchemy ORM models in storage/models.py map to these.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from bifrost.core.enums import (
    ArtifactClassification,
    ComponentType,
    ConfidenceLevel,
    CorrectionType,
    DependencyType,
    DiscoveryMethod,
    FlowType,
    InterfaceType,
    SegmentType,
)
from bifrost.core.types import (
    URN,
    EntityId,
    EvidenceRef,
    KnowledgeTier,
    MetadataEnvelope,
    Scope,
    Timestamp,
)

# ---- Component ---------------------------------------------------------------


class Component(BaseModel):
    """A software component tracked by Bifrost."""

    id: EntityId
    urn: URN
    repo_id: EntityId
    name: str
    type: ComponentType
    interfaces: list[EntityId]
    dependencies: list[EntityId]
    metadata: MetadataEnvelope
    created_at: Timestamp
    updated_at: Timestamp


# ---- Interface ---------------------------------------------------------------


class BifrostInterface(BaseModel):
    """An interface exposed by a component."""

    id: EntityId
    urn: URN
    component_id: EntityId
    type: InterfaceType
    name: str
    spec_ref: str | None = None
    consumers: list[EntityId]
    metadata: MetadataEnvelope
    created_at: Timestamp
    updated_at: Timestamp


# ---- Flow --------------------------------------------------------------------


class FlowSegment(BaseModel):
    """One step in a traced flow."""

    order: int
    component_urn: URN
    interface_urn: URN | None = None
    repo_id: EntityId
    description: str | None = None
    segment_type: SegmentType


class FlowReference(BaseModel):
    """An end-to-end flow spanning one or more components."""

    id: EntityId
    urn: URN
    name: str
    flow_type: FlowType
    segments: list[FlowSegment]
    scope: Scope
    source_basis: list[EvidenceRef]
    metadata: MetadataEnvelope
    created_at: Timestamp
    updated_at: Timestamp


# ---- Dependency --------------------------------------------------------------


class DependencyReference(BaseModel):
    """A directed dependency between two entities."""

    id: EntityId
    urn: URN
    source_id: EntityId
    source_urn: URN
    target_id: EntityId
    target_urn: URN
    dep_type: DependencyType
    confidence: ConfidenceLevel
    discovery_method: DiscoveryMethod
    metadata: MetadataEnvelope
    created_at: Timestamp
    updated_at: Timestamp


# ---- Knowledge Artifact ------------------------------------------------------


class KnowledgeArtifact(BaseModel):
    """A generated or curated knowledge document."""

    id: EntityId
    urn: URN
    tier: KnowledgeTier
    classification: ArtifactClassification
    title: str
    content: str
    content_format: Literal["markdown", "json", "structured"]
    target_urn: URN
    metadata: MetadataEnvelope
    publication_state: Literal["draft", "internal", "generated", "published"]
    created_at: Timestamp
    updated_at: Timestamp


# ---- Gap Report --------------------------------------------------------------


class GapReport(BaseModel):
    """Captures a knowledge gap discovered during a query."""

    id: str
    entity_type: Literal["gap_report"] = "gap_report"
    scope: EntityId
    query_id: str
    description: str
    severity: Literal["critical", "moderate", "minor"]
    suggested_sources: list[str]
    status: Literal["open", "filled", "wont-fill"]
    created_at: Timestamp
    updated_at: Timestamp


# ---- Correction --------------------------------------------------------------


class Correction(BaseModel):
    """Records a correction to existing knowledge."""

    id: EntityId
    entity_id: EntityId
    task_id: str | None = None
    correction_type: CorrectionType
    original_claim: str
    corrected_claim: str
    corrected_by: str
    evidence: list[EvidenceRef]
    status: Literal["pending", "applied", "rejected"]
    created_at: Timestamp
    updated_at: Timestamp
