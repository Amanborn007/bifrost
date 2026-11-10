"""SourceAdapter ABC and supporting types — S2 adapter contract.

Every source integration (Git, Jira, Confluence, etc.) implements
SourceAdapter. The platform never assumes capabilities that the adapter
has not explicitly declared.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel

# ── Source object types ──────────────────────────────────────────────


class SourceObjectType(StrEnum):
    """Source object types across all adapter categories."""

    # Code Truth
    FILE = "file"
    COMMIT = "commit"
    BRANCH = "branch"
    PULL_REQUEST = "pull-request"
    REVIEW = "review"
    REVIEW_COMMENT = "review-comment"
    # Work Intent
    ISSUE = "issue"
    EPIC = "epic"
    SPRINT = "sprint"
    ISSUE_COMMENT = "issue-comment"
    # Design Intent
    ADR = "adr"
    DESIGN_DOC = "design-doc"
    CONFLUENCE_PAGE = "confluence-page"
    # Config / Schema
    CONFIG_FILE = "config-file"
    SCHEMA = "schema"
    API_SPEC = "api-spec"


class SourceCategory(StrEnum):
    """Category of source system."""

    CODE_TRUTH = "code-truth"
    WORK_INTENT = "work-intent"
    DESIGN_INTENT = "design-intent"
    COMMUNICATION_CONTEXT = "communication-context"
    CONFIG_SCHEMA_TRUTH = "config-schema-truth"
    RUNTIME_OBSERVATION = "runtime-observation"
    DEPLOYMENT_STATE = "deployment-state"
    OWNERSHIP_METADATA = "ownership-metadata"


# ── Scope and filtering ─────────────────────────────────────────────


class ScopeFilter(BaseModel):
    """Filter criteria for narrowing adapter scope."""

    path_pattern: str | None = None
    object_types: list[SourceObjectType] | None = None
    modified_after: datetime | None = None
    modified_before: datetime | None = None


class AdapterScope(BaseModel):
    """Narrows adapter operations to a specific boundary within the source."""

    repo_id: str | None = None
    domain_id: str | None = None
    source_scope: dict[str, Any]
    filter: ScopeFilter | None = None


# ── Source references and metadata ───────────────────────────────────


class SourceObjectRef(BaseModel):
    """Globally unique reference to a source object.

    URN format: bifrost:<adapter-id>:<source-instance>:<object-type>:<object-id>
    """

    urn: str
    adapter_id: str
    source_instance: str
    object_type: SourceObjectType
    object_id: str


class SourceMetadata(BaseModel):
    """Metadata about a source object."""

    created_at: datetime | None = None
    modified_at: datetime | None = None
    author: str | None = None
    size_bytes: int | None = None
    content_type: str | None = None
    extra: dict[str, Any] | None = None


class RelevanceHint(BaseModel):
    """Hint about how a source object relates to a Bifrost entity."""

    target_kind: Literal["repo", "domain", "concept", "flow", "dependency"]
    target_id: str
    confidence: Literal["high", "medium", "low"]
    basis: Literal[
        "explicit-config",
        "path-convention",
        "content-reference",
        "label-match",
        "inferred",
    ]


class SourceObject(BaseModel):
    """A discovered source object with metadata but not full content."""

    ref: SourceObjectRef
    name: str
    category: SourceCategory
    metadata: SourceMetadata
    relevance_hints: list[RelevanceHint] | None = None


# ── Content ──────────────────────────────────────────────────────────


class ContentBody(BaseModel):
    """Content in one of several forms, discriminated on 'kind'."""

    kind: Literal["text", "structured", "binary-ref", "composite"]
    text: str | None = None
    encoding: str | None = None
    data: dict[str, Any] | None = None
    location: str | None = None
    size_bytes: int | None = None
    parts: list[ContentBody] | None = None


class RevisionInfo(BaseModel):
    """Revision information for freshness tracking."""

    ref: SourceObjectRef
    revision_token: str
    revision_timestamp: datetime
    parent_revision_token: str | None = None
    change_summary: str | None = None


class ExtractedContent(BaseModel):
    """Full extracted content for a source object."""

    ref: SourceObjectRef
    revision: RevisionInfo
    content: ContentBody
    metadata: SourceMetadata
    relevance_hints: list[RelevanceHint]
    extracted_at: datetime
    truncated: bool | None = None
    is_summary: bool | None = None
    original_token_estimate: int | None = None


# ── Permissions ──────────────────────────────────────────────────────


class UserContext(BaseModel):
    """Context about the user making a request."""

    user_id: str
    source_identity: dict[str, Any] | None = None


class PermissionResult(BaseModel):
    """Result of a permission check."""

    can_read: bool
    determined: bool
    reason: str | None = None


# ── Capabilities and health ──────────────────────────────────────────


class AdapterCapabilities(BaseModel):
    """Declares what operations an adapter supports."""

    supports_watch: bool
    supports_incremental_identify: bool
    supports_change_summary: bool
    supports_permission_check: bool
    supported_object_types: list[SourceObjectType]
    source_category: SourceCategory
    max_concurrent_extractions: int
    supports_partial_extraction: bool


class QuotaInfo(BaseModel):
    """Remaining API quota information."""

    remaining: int
    limit: int
    resets_at: datetime


class HealthStatus(BaseModel):
    """Health check result for an adapter."""

    status: Literal["healthy", "degraded", "unhealthy"]
    latency_ms: float
    message: str | None = None
    checked_at: datetime
    remaining_quota: QuotaInfo | None = None


# ── SourceAdapter ABC ────────────────────────────────────────────────


class SourceAdapter(ABC):
    """Abstract base class for all source integrations.

    Adapters are the sensory boundary between Bifrost and external systems.
    All methods are async. Rate limit errors must be absorbed internally
    (exponential backoff, up to 3 retries).
    """

    @property
    @abstractmethod
    def adapter_id(self) -> str:
        """Unique identifier for this adapter type (e.g., 'github', 'jira')."""
        ...

    @abstractmethod
    def discover(self, scope: AdapterScope) -> AsyncIterator[SourceObject]:
        """Yield every discoverable SourceObject within a scope."""
        ...

    @abstractmethod
    async def identify(self, scope: AdapterScope) -> list[SourceObjectRef]:
        """Return lightweight refs within a scope without fetching content."""
        ...

    @abstractmethod
    async def extract(self, ref: SourceObjectRef) -> ExtractedContent:
        """Extract full content and metadata for a single source object."""
        ...

    @abstractmethod
    async def track_revision(self, ref: SourceObjectRef) -> RevisionInfo:
        """Return current revision info without full extraction."""
        ...

    @abstractmethod
    async def check_permission(self, user: UserContext, ref: SourceObjectRef) -> PermissionResult:
        """Check whether a user has access to the referenced source object."""
        ...

    @abstractmethod
    def get_capabilities(self) -> AdapterCapabilities:
        """Declare the capabilities this adapter supports."""
        ...

    @abstractmethod
    async def health_check(self) -> HealthStatus:
        """Check the health of the connection to the source system."""
        ...
