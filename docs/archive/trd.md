# Bifrost V1 — Technical Requirements Document (TRD)

**Companion to:** docs/archive/prd.md (PRD), docs/archive/specs/ (design specs)
**Status:** Pre-implementation plan, kept because code cites its section numbers. The built system is described in docs/handbook/ (layout 02 §5, schema 03, API 04, config 10).

---

## 1. Package Architecture

The built layout is in the How it works table of [README.md](../../README.md#how-it-works) and in [handbook 02 §5](../handbook/02-architecture.md).

---

## 2. Core Python Interfaces

Planned shapes. Built in core/types.py, core/enums.py, core/models.py, agents/runtime.py, adapters/base.py, storage/backend.py. AgentEvent, AgentResponse and MigrationState were not built; Interface is BifrostInterface (core/models.py).

### 2.1 Primitive Types

```python
from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal, TypeAlias

# UUID v7 -- time-sortable, used as all primary keys
EntityId: TypeAlias = uuid.UUID

# bifrost:{scope}:{type}:{id} -- globally unique across all deployments
URN: TypeAlias = str

# ISO 8601 timestamp (always timezone-aware in runtime)
Timestamp: TypeAlias = datetime

# 0=source-mapping, 1=extracted, 2=derived, 3=generated
KnowledgeTier: TypeAlias = Literal[0, 1, 2, 3]

# Broadest scope a knowledge artifact or flow spans
Scope: TypeAlias = Literal["repo", "domain", "org"]
```

### 2.2 ConfidenceLevel

```python
from __future__ import annotations

from enum import StrEnum


class ConfidenceLevel(StrEnum):
    """Canonical confidence taxonomy for all knowledge artifacts and agent responses.

    Used in entity metadata, task results, agent responses, and the orchestration
    confidence gate (S9 SS10.6).
    """

    CONFIRMED = "confirmed"      # directly supported by current, non-stale source evidence
    INFERRED = "inferred"        # derived from patterns or indirect evidence -- not directly observed
    STALE = "stale"              # based on evidence that was once confirmed but may now be outdated
    UNCERTAIN = "uncertain"      # low evidence; best-effort answer with significant doubt
    UNKNOWN = "unknown"          # cannot answer within current knowledge -- explicitly acknowledged gap
    CONFLICTING = "conflicting"  # two or more sources disagree; neither can be dismissed
```

### 2.3 EvidenceRef

```python
from __future__ import annotations

from pydantic import BaseModel

from bifrost.core.confidence import ConfidenceLevel
from bifrost.core.primitives import Timestamp


class LineRange(BaseModel):
    """Inclusive line range within a source file."""

    start: int
    end: int


class EvidenceRef(BaseModel):
    """A pointer to the specific source material that supports a claim.

    Every finding in an agent response MUST carry at least one EvidenceRef.
    The snippet field is required when line_range is provided.
    """

    source_id: str          # external source identifier (repo URL, issue key, etc.)
    adapter_id: str         # which adapter produced this (e.g., 'git', 'jira')
    file_ref: str | None = None  # file path within the source (e.g., 'src/services/dispatcher.py')
    line_range: LineRange | None = None
    snippet: str | None = None   # the exact excerpt from source that supports the claim
    confidence: ConfidenceLevel
    extracted_at: Timestamp      # when this evidence was last retrieved from source
```

### 2.4 MetadataEnvelope

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from bifrost.core.confidence import ConfidenceLevel
from bifrost.core.evidence import EvidenceRef
from bifrost.core.primitives import Scope, Timestamp


class FreshnessInfo(BaseModel):
    """Freshness state embedded in every MetadataEnvelope."""

    indexed_at: Timestamp
    source_revision: str | None = None  # commit SHA or equivalent
    last_checked_at: Timestamp          # most recent freshness validation
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
    score: float = Field(ge=0.0, le=1.0)  # 0.0-1.0 numeric score
    basis: str  # human-readable explanation of confidence derivation


class KnownGap(BaseModel):
    """A documented gap in knowledge coverage."""

    description: str
    severity: Literal["critical", "moderate", "minor"]
    suggested_source: str | None = None


class MetadataEnvelope(BaseModel):
    """Standard quality envelope carried by every KnowledgeArtifact.

    Encodes provenance, freshness, confidence, scope, and known gaps.
    """

    created_at: Timestamp
    updated_at: Timestamp
    created_by: str  # 'system' | 'agent:{agentId}' | 'user:{userId}'
    version: int     # monotonically increasing; used for optimistic concurrency
    source_refs: list[EvidenceRef]  # all source evidence supporting this artifact

    freshness: FreshnessInfo
    confidence: ConfidenceInfo
    scope: Scope
    known_gaps: list[KnownGap] = Field(default_factory=list)
```

### 2.5 AgentTask

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TaskScope(BaseModel):
    """Scope definition for an agent task."""

    level: Literal["repo", "domain", "org", "flow", "specific"]
    repo_ids: list[str] | None = None
    domain_ids: list[str] | None = None
    flow_ids: list[str] | None = None
    entity_refs: list[str] | None = None


class TaskBudget(BaseModel):
    """Resource budget constraints for a task."""

    max_llm_calls: int = 20
    max_wall_time_ms: int = 30_000
    max_agents_consulted: int = 5


class TaskContext(BaseModel):
    """Optional context passed to an agent task."""

    prior_findings: str | None = None       # summary of prior agent findings (sequential pattern)
    focus_areas: list[str] | None = None    # areas the coordinator wants emphasized
    requester_type: Literal["human", "ai_agent"] = "human"


class FreshnessRequirement(BaseModel):
    """Freshness constraints for a task."""

    max_staleness_ms: int | None = None
    require_live_fetch: bool = False


# Task type discriminator
TaskType = Literal[
    "understand",      # explain what something is or how it works
    "plan",            # produce an implementation or migration plan
    "validate",        # check a proposal against existing state
    "compare",         # compare implementations, flows, or options
    "trace_flow",      # trace an end-to-end flow
    "assess_impact",   # determine blast radius of a change
    "generate_doc",    # produce or refresh documentation
    "refresh",         # re-extract stale knowledge for a scope
    "extract",         # initial extraction (used by ExtractionAgent only)
]


class AgentTask(BaseModel):
    """The unit of work submitted to AgentRuntime.execute().

    Track A: passed to in-process Python agent class.
    Track B: serialized and sent to Managed Agent Session as initial message.
    """

    task_id: str
    type: TaskType
    query: str  # natural language question or structured request
    scope: TaskScope
    budget: TaskBudget = Field(default_factory=TaskBudget)
    context: TaskContext | None = None
    freshness_requirement: FreshnessRequirement | None = None
```

### 2.6 AgentEvent

```python
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from bifrost.core.agent_response import AgentResponse
from bifrost.core.primitives import Timestamp


class ThinkingEvent(BaseModel):
    """Agent's internal reasoning (shown to operators, not end users)."""

    type: Literal["thinking"] = "thinking"
    task_id: str
    timestamp: Timestamp
    thought: str


class ToolUseEvent(BaseModel):
    """Agent is invoking a tool."""

    type: Literal["tool_use"] = "tool_use"
    task_id: str
    timestamp: Timestamp
    tool_name: str  # e.g., 'read_knowledge', 'extract_file', 'query_entities'
    tool_input: dict[str, Any]


class ToolResultEvent(BaseModel):
    """Result returned from a tool invocation."""

    type: Literal["tool_result"] = "tool_result"
    task_id: str
    timestamp: Timestamp
    tool_name: str
    result: dict[str, Any]
    tokens_consumed: int


class ResultEvent(BaseModel):
    """Final successful result from an agent consultation."""

    type: Literal["result"] = "result"
    task_id: str
    timestamp: Timestamp
    response: AgentResponse


class ErrorEvent(BaseModel):
    """Agent-level error event."""

    type: Literal["error"] = "error"
    task_id: str
    timestamp: Timestamp
    code: str
    message: str
    retryable: bool
    detail: Any | None = None


# Discriminated union of all agent events.
# Consumers switch on the `type` field.
# Track A: emitted by AsyncGenerator in-process.
# Track B: deserialized from Managed Agent Session SSE stream.
AgentEvent = ThinkingEvent | ToolUseEvent | ToolResultEvent | ResultEvent | ErrorEvent
```

### 2.7 AgentResponse

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from bifrost.core.confidence import ConfidenceLevel
from bifrost.core.evidence import EvidenceRef
from bifrost.core.primitives import Timestamp


class Finding(BaseModel):
    """A single finding within an agent response."""

    id: str
    summary: str
    detail: str
    confidence: ConfidenceLevel
    source_refs: list[EvidenceRef]


class ResponseFreshness(BaseModel):
    """Freshness metadata for an agent response."""

    last_source_check: Timestamp
    last_knowledge_update: Timestamp
    lag_seconds: int | None = None
    is_stale: bool
    stale_reason: str | None = None


class ResponseGap(BaseModel):
    """A knowledge gap identified during agent processing."""

    description: str
    possible_sources: list[str]
    impact: Literal["blocking", "reduces_confidence", "informational"]


class UsageInfo(BaseModel):
    """LLM usage statistics for an agent response."""

    model: str
    input_tokens: int
    output_tokens: int
    cost_estimate_usd: float


class ResponseFeedback(BaseModel):
    """Feedback information attached to an agent response."""

    gap_reports: list[GapReport]
    corrections: list[Correction]
    quality_signal: Literal["helpful", "partially-helpful", "unhelpful"] | None = None


class AgentResponse(BaseModel):
    """The final structured response from an agent consultation.

    Required on all agent implementations -- Track A and Track B.
    Validated against this schema by the orchestrator before storage.
    """

    findings: list[Finding]

    evidence: list[EvidenceRef]  # all evidence across all findings, deduplicated

    confidence: ConfidenceLevel  # aggregate confidence across all findings

    freshness: ResponseFreshness

    gaps: list[ResponseGap]

    coverage_level: Literal["full", "partial", "minimal", "none"]

    context_truncated: bool
    excluded_artifacts: list[str] | None = None  # IDs of artifacts excluded due to context budget

    usage: UsageInfo

    feedback: ResponseFeedback | None = None
```

> **Note:** `GapReport` and `Correction` referenced in `ResponseFeedback` are defined in Section 2.13 below.

### 2.8 AgentRuntime

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from bifrost.core.agent_event import AgentEvent
from bifrost.core.agent_task import AgentTask


class AgentRuntime(ABC):
    """Abstract execution interface for all agent types.

    Track A: implemented by in-process Python classes (RepoAgent, TaskCoordinator, ExtractionAgent).
    Track B: implemented by ManagedAgentRuntime that delegates to Anthropic Managed Agent Sessions.

    The single method contract ensures Track A -> B migration requires zero business logic changes.
    """

    # Track B only -- None in Track A.
    session_id: str | None = None

    @abstractmethod
    async def execute(self, task: AgentTask) -> AsyncIterator[AgentEvent]:
        """Execute a task, yielding structured events as an async iterator.

        The final event MUST be type='result' (success) or type='error' (failure).
        Intermediate events (thinking, tool_use, tool_result) are optional but recommended.
        """
        ...

    @abstractmethod
    def get_definition_id(self) -> str:
        """Returns the agent's definition identifier.

        Track A: class name (e.g., 'RepoAgent').
        Track B: Anthropic agent_definition_id.
        """
        ...
```

### 2.9 AgentStateBackend

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class WritePrecondition:
    """Precondition for AgentStateBackend.write().

    sha: write fails with ConflictError if current content_hash differs.
    must_not_exist: write fails with ConflictError if document already exists.
    """

    sha: str | None = None
    must_not_exist: bool = False


@dataclass(frozen=True)
class DocumentEntry:
    """Lightweight reference returned by AgentStateBackend.list()."""

    path: str
    updated_at: datetime
    content_hash: str


class AgentStateBackend(ABC):
    """Abstract storage interface for agent state (knowledge text, freshness docs, configs).

    Track A: implemented by PostgresStateBackend (knowledge_text_cache table).
    Track B: implemented by MemoryStoreBackend (Anthropic Memory Stores API).

    Path conventions:
      /repo/{id}/components/{name}.md       -- component knowledge text
      /repo/{id}/flows/{flow-id}.md         -- flow knowledge text
      /repo/{id}/dependencies/{dep-id}.md   -- dependency knowledge text
      /repo/{id}/freshness/state.md         -- repo freshness state document
      /repo/{id}/config.md                  -- agent config
      /repo/{id}/index.md                   -- index of all indexed entities
    """

    @abstractmethod
    async def read(self, path: str) -> str | None:
        """Read a document by exact path. Returns None if not found."""
        ...

    @abstractmethod
    async def write(
        self,
        path: str,
        content: str,
        precondition: WritePrecondition | None = None,
    ) -> None:
        """Write a document at the given path.

        If precondition.sha is provided: write fails with ConflictError if
        current content_hash differs.
        If precondition.must_not_exist is True: write fails with ConflictError
        if document already exists.
        """
        ...

    @abstractmethod
    async def list(self, path_prefix: str) -> list[DocumentEntry]:
        """List all document paths with the given prefix.

        Returns path, updated_at, and content_hash for each match.
        """
        ...

    @abstractmethod
    async def delete(self, path: str) -> None:
        """Delete a document. No-op if not found."""
        ...
```

### 2.10 SourceAdapter

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from bifrost.core.primitives import URN


class SourceObjectType(StrEnum):
    """Types of objects a source adapter can discover."""

    FILE = "file"
    DIRECTORY = "directory"
    COMMIT = "commit"
    PULL_REQUEST = "pull-request"
    ISSUE = "issue"
    PAGE = "page"
    MESSAGE = "message"


class AdapterScope(BaseModel):
    """Scope for adapter discovery and watch operations."""

    source_type: str  # 'github', 'jira', 'confluence'
    repo_url: str | None = None
    project_keys: list[str] | None = None
    object_types: list[SourceObjectType] | None = None


class SourceObjectRef(BaseModel):
    """Lightweight reference to a source object."""

    urn: URN
    adapter_id: str
    source_type: str
    object_type: SourceObjectType
    external_id: str  # adapter-native identifier (path, issue key, etc.)


class SourceObject(BaseModel):
    """A discovered source object with basic metadata."""

    ref: SourceObjectRef
    name: str
    last_modified: datetime | None = None
    size_bytes: int | None = None
    metadata: dict[str, Any] = {}


class AdapterCapabilities(BaseModel):
    """Declares which features a source adapter supports."""

    supports_watch: bool
    supports_incremental: bool
    supports_permission_check: bool
    supported_object_types: list[SourceObjectType]


class AdapterHealthStatus(BaseModel):
    """Health check result from a source adapter."""

    status: Literal["healthy", "degraded", "unhealthy"]
    latency_ms: float
    error_message: str | None = None
    remaining_quota: int | None = None
    rate_limit_reset_at: datetime | None = None


class ChangeEvent(BaseModel):
    """Event emitted by a watch subscription."""

    ref: SourceObjectRef
    change_type: Literal["created", "updated", "deleted"]
    new_revision: str | None = None
    detected_at: datetime


class Subscription(Protocol):
    """Handle returned by SourceAdapter.watch() to unsubscribe."""

    def unsubscribe(self) -> None: ...


# Callback type for watch events
ChangeCallback = Callable[[ChangeEvent], None]


class SourceAdapter(ABC):
    """Plugin interface for source system connectors (Git, Jira, Confluence, etc.).

    All adapters must implement rate-limit absorption and content size validation
    internally. Adapters MUST NOT surface HTTP 429 errors to callers -- absorb and
    retry (3 attempts, 1s/2s/4s).
    """

    @property
    @abstractmethod
    def adapter_id(self) -> str:
        """Unique adapter type identifier (e.g., 'git', 'jira', 'confluence')."""
        ...

    @abstractmethod
    async def discover(self, scope: AdapterScope) -> AsyncIterator[SourceObject]:
        """Walk the source and yield all discoverable SourceObjects.

        AsyncIterator to handle large source trees without unbounded memory.
        """
        ...

    @abstractmethod
    async def identify(self, scope: AdapterScope) -> list[SourceObjectRef]:
        """Return lightweight refs for all objects in scope.

        Used for inventory and diff.
        """
        ...

    @abstractmethod
    async def extract(self, ref: SourceObjectRef) -> ExtractedContent:
        """Extract full structured content for a single source object.

        MUST validate content size against AdapterContentPolicy before returning.
        MUST absorb rate limits (with_retry wrapper required).
        """
        ...

    @abstractmethod
    async def track_revision(self, ref: SourceObjectRef) -> RevisionInfo:
        """Return the current revision token without full extraction.

        Used by the Freshness Engine (S3) to detect changes cheaply.
        """
        ...

    def watch(
        self,
        scope: AdapterScope,
        callback: ChangeCallback,
    ) -> Subscription:
        """Optional: subscribe to real-time change events (webhooks, event streams).

        Implementations that support this set get_capabilities().supports_watch = True.
        Callback is called within 5 minutes of a source commit.

        Default implementation raises NotImplementedError.
        """
        raise NotImplementedError(
            f"{self.adapter_id} adapter does not support watch"
        )

    @abstractmethod
    def get_capabilities(self) -> AdapterCapabilities:
        """Declare the capabilities this adapter supports.

        Used by platform to determine valid operations and routing.
        """
        ...

    @abstractmethod
    async def get_health(self) -> AdapterHealthStatus:
        """Health check: returns connection status, latency, and quota state."""
        ...
```

### 2.11 ExtractedContent

```python
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from bifrost.core.source_adapter import SourceObjectRef


class TextContent(BaseModel):
    """Plain text content body."""

    kind: Literal["text"] = "text"
    text: str
    encoding: str | None = None


class StructuredContent(BaseModel):
    """Structured (JSON-like) content body."""

    kind: Literal["structured"] = "structured"
    data: dict[str, Any]


class BinaryRefContent(BaseModel):
    """Reference to binary content stored externally."""

    kind: Literal["binary-ref"] = "binary-ref"
    location: str
    size_bytes: int


class CompositeContent(BaseModel):
    """Composite content composed of multiple ContentBody parts."""

    kind: Literal["composite"] = "composite"
    parts: list[ContentBody]


# Discriminated union of all content body types
ContentBody = Annotated[
    TextContent | StructuredContent | BinaryRefContent | CompositeContent,
    Field(discriminator="kind"),
]

# Update forward ref for CompositeContent
CompositeContent.model_rebuild()


class RevisionInfo(BaseModel):
    """Revision metadata for a source object."""

    ref: SourceObjectRef
    revision_token: str            # commit SHA, ETag, sequence number, etc.
    revision_timestamp: datetime
    parent_revision_token: str | None = None
    change_summary: str | None = None


class SourceMetadata(BaseModel):
    """Metadata about the source material."""

    title: str | None = None
    author: str | None = None
    language: str | None = None      # programming language for code files
    line_count: int | None = None
    tags: list[str] | None = None
    permissions: list[str] | None = None  # access groups that can view this source


class ExtractedContent(BaseModel):
    """Output of SourceAdapter.extract().

    Consumed by the extraction pipeline (bifrost.extraction) as input to LLM analysis.
    Content size is bounded by AdapterContentPolicy (default: 50K tokens).
    """

    ref: SourceObjectRef
    revision: RevisionInfo
    content: ContentBody
    metadata: SourceMetadata
    extracted_at: datetime
    truncated: bool = False         # True if content was cut to fit token budget
    is_summary: bool = False        # True if content replaced with structural summary
    token_estimate: int | None = None  # estimated token count (set when truncated or is_summary)
```

### 2.12 TaskResult

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from bifrost.core.confidence import ConfidenceLevel
from bifrost.core.evidence import EvidenceRef
from bifrost.core.primitives import Timestamp


class TaskFinding(BaseModel):
    """A single finding within a task result."""

    id: str
    summary: str
    detail: str
    confidence: ConfidenceLevel
    source_refs: list[EvidenceRef]


class TaskGap(BaseModel):
    """A knowledge gap discovered during task processing."""

    description: str
    possible_sources: list[str]
    impact: Literal["blocking", "reduces_confidence", "informational"]


class ConflictSide(BaseModel):
    """One side of a conflicting knowledge claim."""

    agent_id: str
    claim: str
    evidence: list[EvidenceRef]


class TaskConflict(BaseModel):
    """A conflict between multiple agent findings."""

    description: str
    sides: list[ConflictSide]
    resolution: Literal["unresolved", "resolved_by_evidence", "flagged_for_human"]


class TaskUsage(BaseModel):
    """Aggregate LLM usage statistics for a completed task."""

    total_llm_calls: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    agents_consulted: list[str]
    wall_time_ms: int


class TaskResult(BaseModel):
    """The final result of a completed task.

    Stored in the tasks table and returned by
    GET /api/v1/tasks/:id when status='delivered'.
    """

    task_id: str
    status: Literal["delivered", "failed", "partial"]

    answer: str  # synthesized prose answer (markdown)

    findings: list[TaskFinding]

    confidence: ConfidenceLevel
    coverage_level: Literal["full", "partial", "minimal", "none"]

    source_refs: list[EvidenceRef]  # all evidence across all findings

    consultation_log_ids: list[str]  # IDs of task_consultation_log rows

    gaps: list[TaskGap]

    conflicts: list[TaskConflict]

    budget_exhausted: bool
    budget_exhausted_detail: str | None = None

    usage: TaskUsage

    track: Literal["A", "B"]  # which execution track produced this result
```

### 2.13 Entity Types

```python
from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

from bifrost.core.confidence import ConfidenceLevel
from bifrost.core.evidence import EvidenceRef
from bifrost.core.metadata import MetadataEnvelope
from bifrost.core.primitives import (
    EntityId,
    KnowledgeTier,
    Scope,
    Timestamp,
    URN,
)


# ---- Component ---------------------------------------------------------------

class ComponentType(StrEnum):
    MODULE = "module"
    SERVICE = "service"
    LIBRARY = "library"
    PACKAGE = "package"


class Component(BaseModel):
    """A software component tracked by Bifrost."""

    id: EntityId
    urn: URN                        # bifrost:repo:{repo_id}:component:{id}
    repo_id: EntityId
    name: str
    type: ComponentType
    interfaces: list[EntityId]      # FK[] to BifrostInterface
    dependencies: list[EntityId]    # FK[] to DependencyReference
    metadata: MetadataEnvelope
    created_at: Timestamp
    updated_at: Timestamp


# ---- Interface ---------------------------------------------------------------

class InterfaceType(StrEnum):
    API = "api"
    EVENT = "event"
    SCHEMA = "schema"
    CONFIG = "config"


class BifrostInterface(BaseModel):
    """An interface exposed by a component.

    Named BifrostInterface to avoid collision with Python builtins.
    """

    id: EntityId
    urn: URN                        # bifrost:repo:{repo_id}:interface:{id}
    component_id: EntityId
    type: InterfaceType
    name: str
    spec_ref: str | None = None     # URL or path to OpenAPI spec, protobuf, Avro schema
    consumers: list[EntityId]       # FK[] to Component (cross-repo via URN allowed)
    metadata: MetadataEnvelope
    created_at: Timestamp
    updated_at: Timestamp


# ---- FlowReference -----------------------------------------------------------

class FlowType(StrEnum):
    REQUEST_RESPONSE = "request-response"
    EVENT_DRIVEN = "event-driven"
    BATCH = "batch"
    SAGA = "saga"
    PIPELINE = "pipeline"
    MIXED = "mixed"


class SegmentType(StrEnum):
    SYNC_CALL = "sync-call"
    ASYNC_MESSAGE = "async-message"
    DB_READ = "db-read"
    DB_WRITE = "db-write"
    CACHE_READ = "cache-read"
    CACHE_WRITE = "cache-write"
    EXTERNAL_API = "external-api"


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
    urn: URN                        # bifrost:{scope}:flow:{id}
    name: str
    flow_type: FlowType
    segments: list[FlowSegment]
    scope: Scope
    source_basis: list[EvidenceRef]
    metadata: MetadataEnvelope
    created_at: Timestamp
    updated_at: Timestamp


# ---- DependencyReference ------------------------------------------------------

class DependencyType(StrEnum):
    CODE = "code"
    API = "api"
    SCHEMA = "schema"
    CONFIG = "config"
    EVENT = "event"
    RUNTIME = "runtime"
    OWNERSHIP = "ownership"
    MIGRATION = "migration"
    INFERRED_HIDDEN = "inferred-hidden"


class DiscoveryMethod(StrEnum):
    STATIC_ANALYSIS = "static-analysis"
    SPEC_PARSING = "spec-parsing"
    RUNTIME_OBSERVATION = "runtime-observation"
    MANUAL = "manual"
    INFERRED = "inferred"


class DependencyReference(BaseModel):
    """A directed dependency between two entities."""

    id: EntityId
    urn: URN
    source_id: EntityId             # the entity that depends
    source_urn: URN
    target_id: EntityId             # the entity depended upon
    target_urn: URN
    dep_type: DependencyType
    confidence: ConfidenceLevel
    discovery_method: DiscoveryMethod
    metadata: MetadataEnvelope
    created_at: Timestamp
    updated_at: Timestamp


# ---- KnowledgeArtifact --------------------------------------------------------

class ArtifactClassification(StrEnum):
    REPO_OVERVIEW = "repo-overview"
    DOMAIN_SUMMARY = "domain-summary"
    FLOW_DOCUMENTATION = "flow-documentation"
    ONBOARDING_GUIDE = "onboarding-guide"
    IMPLEMENTATION_PLAYBOOK = "implementation-playbook"
    MIGRATION_GUIDE = "migration-guide"
    DEPENDENCY_ANALYSIS = "dependency-analysis"
    RISK_ASSESSMENT = "risk-assessment"
    PATTERN_ANALYSIS = "pattern-analysis"
    DECISION_RECORD = "decision-record"
    CUSTOM = "custom"


class KnowledgeArtifact(BaseModel):
    """A generated or curated knowledge document."""

    id: EntityId
    urn: URN
    tier: KnowledgeTier
    classification: ArtifactClassification
    title: str
    content: str
    content_format: Literal["markdown", "json", "structured"]
    target_urn: URN                 # which entity this artifact describes
    metadata: MetadataEnvelope
    publication_state: Literal["draft", "internal", "generated", "published"]
    created_at: Timestamp
    updated_at: Timestamp


# ---- GapReport ----------------------------------------------------------------

class GapReport(BaseModel):
    """T1 entity: captures a knowledge gap discovered during a query.

    Written on every agent consultation that identifies missing information.
    Used for V2 training signal and continuous knowledge improvement.
    """

    id: str                         # bifrost:gap-report:{scope}:{id}
    entity_type: Literal["gap_report"] = "gap_report"
    scope: EntityId                 # repo_id or domain_id
    query_id: str                   # task_id that surfaced this gap
    description: str                # what knowledge was missing
    severity: Literal["critical", "moderate", "minor"]
    suggested_sources: list[str]    # adapters or repos that might fill the gap
    status: Literal["open", "filled", "wont-fill"]
    created_at: Timestamp
    updated_at: Timestamp


# ---- Correction ---------------------------------------------------------------

class CorrectionType(StrEnum):
    FACTUAL_ERROR = "factual-error"
    OUTDATED_CLAIM = "outdated-claim"
    MISSING_CONTEXT = "missing-context"
    WRONG_CONFIDENCE = "wrong-confidence"
    HALLUCINATION = "hallucination"


class Correction(BaseModel):
    """Records a correction to existing knowledge, from user, agent, or validation.

    Written on every user-submitted correction via POST /api/v1/feedback.
    Used for V2 training signal.
    """

    id: EntityId
    entity_id: EntityId             # the entity being corrected
    task_id: str | None = None      # the task that surfaced the incorrect claim
    correction_type: CorrectionType
    original_claim: str
    corrected_claim: str
    corrected_by: str               # 'user:{id}' | 'agent:{id}' | 'validator'
    evidence: list[EvidenceRef]
    status: Literal["pending", "applied", "rejected"]
    created_at: Timestamp
    updated_at: Timestamp


# ---- MigrationState -----------------------------------------------------------

class MigrationState(BaseModel):
    """Tracks the Track A -> Track B migration state for a given repo or domain.

    Written when BIFROST_TRACK switches from A to B.
    Used by the migration job to ensure all knowledge_text_cache rows are
    uploaded to Anthropic Memory Stores before Track A data is retired.
    """

    id: EntityId
    scope_id: str                   # repo_id or 'global'
    scope_type: Literal["repo", "global"]
    from_track: Literal["A"] = "A"
    to_track: Literal["B"] = "B"
    status: Literal[
        "not_started",
        "in_progress",
        "complete",
        "failed",
        "rolled_back",
    ]
    rows_total: int
    rows_migrated: int
    rows_failed: int
    started_at: Timestamp | None = None
    completed_at: Timestamp | None = None
    error: str | None = None
    created_at: Timestamp
    updated_at: Timestamp
```


---

## 3. Database Schema

The built schema is in `alembic/versions/001_baseline.py` and `src/bifrost/storage/models.py`, and is described in [handbook 03](../handbook/03-data-model.md). It has 52 tables. 35 are mapped as ORM models in `models.py`, and 17 exist only in the migration. The column-level tables that were planned here are not kept.

The 12 V1 tables planned in §3.1 to §3.12 were:

- `source_repositories`
- `domains`
- `entities`
- `entity_relationships`
- `knowledge_text_cache`
- `agent_configs`
- `repo_freshness`
- `tasks`
- `task_consultation_log`
- `llm_cost_log`
- `agent_session_events`
- `global_rules`

`source_repositories` became `repos`, and `tasks` became `extraction_tasks`.

---

## 4. API Contracts

Base URL: `/api/v1/`. All responses use the standard envelope from S10:

```python
# bifrost/api/schemas/envelope.py

from datetime import datetime
from typing import Generic, TypeVar, Literal
from pydantic import BaseModel, Field
from uuid import uuid4

T = TypeVar("T")


class EnvelopeMeta(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    track: Literal["A", "B"] = "A"


class BifrostEnvelope(BaseModel, Generic[T]):
    data: T
    meta: EnvelopeMeta


class ErrorDetail(BaseModel):
    code: str                       # machine-readable (e.g., "BUDGET_EXCEEDED", "NOT_FOUND")
    message: str                    # human-readable
    retryable: bool
    detail: dict | None = None


class BifrostErrorResponse(BaseModel):
    error: ErrorDetail
    meta: EnvelopeMeta
```

**Auth (shared across all authenticated routes):**

Bifrost resolves a principal for every non-public route in `IdentityMiddleware` (`src/bifrost/api/auth/`). `identity.provider` chooses how: `none` treats a loopback caller as the admin operator and needs no header; `header` reads identity from trusted proxy headers; `oidc_jwt` verifies an `Authorization: Bearer` JWT against the configured JWKS and accepts RS256 and ES256 only. Scripts can send `Authorization: Bearer bft_…` tokens minted with `bifrost token new`, which Bifrost matches by sha256 against `api_tokens` in `bifrost.yaml`. Any other bearer value gets 401 `invalid_credentials`, a request with no principal gets 401 `identity_required`, and a principal without the needed role gets 403 `auth.role_required`. The only auth routes are `GET /api/v1/auth/me`, `POST /api/v1/auth/refresh` and `POST /api/v1/auth/logout`. See [RUNNING.md section 6](../../RUNNING.md#6-identity) and [handbook 02 §8.1](../handbook/02-architecture.md#81-auth-at-the-api-boundary).

The route snippets below show the planned `Depends(get_current_user)` dependency, which was not built. The built routes take `current_principal` from `bifrost.api.auth.middleware` and `require_role` from `bifrost.api.deps`.

### 4.1 Repos Endpoints

```python
# bifrost/api/schemas/repos.py

from pydantic import BaseModel
from datetime import datetime


class RegisterRepoRequest(BaseModel):
    name: str
    url: str                                    # remote git URL
    domain_id: str | None = None
    description: str | None = None
    adapter_type: Literal["git", "github"]
    adapter_config: dict[str, str] | None = None  # non-secret adapter config


class RegisterRepoData(BaseModel):
    id: str
    urn: str
    status: Literal["registered"]


class RepoSummary(BaseModel):
    id: str
    urn: str
    name: str
    url: str
    domain_id: str | None
    freshness_state: str
    index_status: str
    last_indexed_at: datetime | None


class RepoFreshness(BaseModel):
    state: str
    last_commit_indexed: str | None
    last_indexed_at: datetime | None
    pending_commits: int


class RepoStats(BaseModel):
    component_count: int
    flow_count: int
    dependency_count: int


class RepoDetail(BaseModel):
    id: str
    urn: str
    name: str
    url: str
    domain_id: str | None
    freshness: RepoFreshness
    stats: RepoStats


class DeregisterRepoData(BaseModel):
    id: str
    status: Literal["deregistered"]


class TriggerExtractionRequest(BaseModel):
    force_full: bool = False                    # ignore cursor and do full extraction
    budget_override: dict | None = None         # {"max_llm_calls": int, "max_wall_time_ms": int}


class TriggerExtractionData(BaseModel):
    task_id: str
    status: Literal["queued"]
```

```python
# bifrost/api/routes/repos.py

from fastapi import APIRouter, Depends, HTTPException, status
from bifrost.api.deps import get_current_user
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.api.schemas.repos import (
    RegisterRepoRequest, RegisterRepoData,
    RepoSummary, RepoDetail,
    DeregisterRepoData,
    TriggerExtractionRequest, TriggerExtractionData,
)

router = APIRouter(prefix="/api/v1/repos", tags=["repos"])


@router.post(
    "",
    response_model=BifrostEnvelope[RegisterRepoData],
    status_code=status.HTTP_201_CREATED,
)
async def register_repo(
    body: RegisterRepoRequest,
    current_user: User = Depends(get_current_user),
) -> BifrostEnvelope[RegisterRepoData]:
    """Register a new repository.
    Errors: 400 (ValidationError), 409 (ConflictError -- URL already registered)"""
    ...


@router.get("", response_model=BifrostEnvelope[list[RepoSummary]])
async def list_repos(
    current_user: User = Depends(get_current_user),
) -> BifrostEnvelope[list[RepoSummary]]:
    """List all registered repos."""
    ...


@router.get("/{repo_id}", response_model=BifrostEnvelope[RepoDetail])
async def get_repo(
    repo_id: str,
    current_user: User = Depends(get_current_user),
) -> BifrostEnvelope[RepoDetail]:
    """Repo detail.
    Errors: 404 (BifrostError -- repo not found)"""
    ...


@router.delete("/{repo_id}", response_model=BifrostEnvelope[DeregisterRepoData])
async def deregister_repo(
    repo_id: str,
    current_user: User = Depends(get_current_user),
) -> BifrostEnvelope[DeregisterRepoData]:
    """Deregister (soft delete).
    Errors: 404"""
    ...


@router.post("/{repo_id}/extract", response_model=BifrostEnvelope[TriggerExtractionData])
async def trigger_extraction(
    repo_id: str,
    body: TriggerExtractionRequest,
    current_user: User = Depends(get_current_user),
) -> BifrostEnvelope[TriggerExtractionData]:
    """Trigger manual extraction.
    Auth: principal required (see the auth paragraph above).
    Errors: 400, 404, 402 (BudgetExceededError)"""
    ...
```

### 4.2 Domains Endpoints

```python
# bifrost/api/schemas/domains.py

from pydantic import BaseModel


class CreateDomainRequest(BaseModel):
    name: str
    description: str | None = None


class CreateDomainData(BaseModel):
    id: str
    urn: str


class DomainSummary(BaseModel):
    id: str
    name: str
    repo_count: int


class DomainRepoEntry(BaseModel):
    id: str
    name: str
    freshness_state: str


class DomainDetail(BaseModel):
    id: str
    name: str
    description: str | None
    repos: list[DomainRepoEntry]
    cross_repo_flow_count: int


class UpdateDomainRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class UpdateDomainData(BaseModel):
    id: str
    updated: Literal[True]


class DeleteDomainData(BaseModel):
    id: str
    status: Literal["deleted"]


class AddRepoDomainRequest(BaseModel):
    repo_id: str


class AddRepoDomainData(BaseModel):
    domain_id: str
    repo_id: str


class RemoveRepoDomainData(BaseModel):
    domain_id: str
    repo_id: str
    removed: Literal[True]
```

```python
# bifrost/api/routes/domains.py

from fastapi import APIRouter, Depends, status
from bifrost.api.deps import get_current_user
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.api.schemas.domains import (
    CreateDomainRequest, CreateDomainData,
    DomainSummary, DomainDetail,
    UpdateDomainRequest, UpdateDomainData,
    DeleteDomainData,
    AddRepoDomainRequest, AddRepoDomainData,
    RemoveRepoDomainData,
)

router = APIRouter(prefix="/api/v1/domains", tags=["domains"])


@router.post(
    "",
    response_model=BifrostEnvelope[CreateDomainData],
    status_code=status.HTTP_201_CREATED,
)
async def create_domain(
    body: CreateDomainRequest,
    current_user: User = Depends(get_current_user),
) -> BifrostEnvelope[CreateDomainData]:
    ...


@router.get("", response_model=BifrostEnvelope[list[DomainSummary]])
async def list_domains(
    current_user: User = Depends(get_current_user),
) -> BifrostEnvelope[list[DomainSummary]]:
    ...


@router.get("/{domain_id}", response_model=BifrostEnvelope[DomainDetail])
async def get_domain(
    domain_id: str,
    current_user: User = Depends(get_current_user),
) -> BifrostEnvelope[DomainDetail]:
    ...


@router.put("/{domain_id}", response_model=BifrostEnvelope[UpdateDomainData])
async def update_domain(
    domain_id: str,
    body: UpdateDomainRequest,
    current_user: User = Depends(get_current_user),
) -> BifrostEnvelope[UpdateDomainData]:
    ...


@router.delete("/{domain_id}", response_model=BifrostEnvelope[DeleteDomainData])
async def delete_domain(
    domain_id: str,
    current_user: User = Depends(get_current_user),
) -> BifrostEnvelope[DeleteDomainData]:
    """Note: does NOT delete repos -- only removes the grouping."""
    ...


@router.post(
    "/{domain_id}/repos",
    response_model=BifrostEnvelope[AddRepoDomainData],
    status_code=status.HTTP_201_CREATED,
)
async def add_repo_to_domain(
    domain_id: str,
    body: AddRepoDomainRequest,
    current_user: User = Depends(get_current_user),
) -> BifrostEnvelope[AddRepoDomainData]:
    ...


@router.delete(
    "/{domain_id}/repos/{repo_id}",
    response_model=BifrostEnvelope[RemoveRepoDomainData],
)
async def remove_repo_from_domain(
    domain_id: str,
    repo_id: str,
    current_user: User = Depends(get_current_user),
) -> BifrostEnvelope[RemoveRepoDomainData]:
    ...
```

### 4.3 Tasks Endpoints

```python
# bifrost/api/schemas/extraction_tasks.py

from pydantic import BaseModel
from datetime import datetime
from typing import Any


class TaskBudget(BaseModel):
    max_llm_calls: int | None = None
    max_wall_time_ms: int | None = None


class TaskScope(BaseModel):
    repo_ids: list[str] | None = None
    domain_id: str | None = None


class SubmitTaskRequest(BaseModel):
    type: str                                   # AgentTask type
    query: str
    scope: TaskScope
    budget: TaskBudget | None = None
    freshness_requirement: str | None = None


class SubmitTaskData(BaseModel):
    task_id: str
    status: Literal["received"]
    stream_url: str                             # /api/v1/tasks/{id}/stream


class TaskSummary(BaseModel):
    id: str
    type: str
    status: str
    query: str
    created_at: datetime
    updated_at: datetime


class TaskDetail(BaseModel):
    id: str
    type: str
    status: str
    query: str
    result: dict[str, Any] | None               # None until status='delivered'
    created_at: datetime
    updated_at: datetime


class CancelTaskData(BaseModel):
    id: str
    status: Literal["cancelled"]
```

```python
# bifrost/api/routes/extraction_tasks.py

from fastapi import APIRouter, Depends, HTTPException, status
from bifrost.api.deps import get_current_user
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.api.schemas.extraction_tasks import (
    SubmitTaskRequest, SubmitTaskData,
    TaskSummary, TaskDetail,
    CancelTaskData,
)

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


@router.post(
    "",
    response_model=BifrostEnvelope[SubmitTaskData],
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_task(
    body: SubmitTaskRequest,
    current_user: User = Depends(get_current_user),
) -> BifrostEnvelope[SubmitTaskData]:
    """Submit a task.
    Auth: principal required (see the auth paragraph above).
    Errors: 400 (ValidationError), 402 (BudgetExceededError)"""
    ...


@router.get("", response_model=BifrostEnvelope[list[TaskSummary]])
async def list_tasks(
    current_user: User = Depends(get_current_user),
) -> BifrostEnvelope[list[TaskSummary]]:
    """List tasks."""
    ...


@router.get("/{task_id}", response_model=BifrostEnvelope[TaskDetail])
async def get_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> BifrostEnvelope[TaskDetail]:
    """Get task result.
    Errors: 404"""
    ...


@router.delete("/{task_id}", response_model=BifrostEnvelope[CancelTaskData])
async def cancel_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> BifrostEnvelope[CancelTaskData]:
    """Cancel task.
    Errors: 404, 409 (task already delivered/failed)"""
    ...
```

### 4.4 SSE Streaming

```python
# bifrost/api/schemas/sse.py

from pydantic import BaseModel
from typing import Literal


SSEEventType = Literal[
    "task.created",             # data: { task_id, status: "received" }
    "status.changed",           # data: { task_id, old_status, new_status }
    "consultation.started",     # data: { task_id, agent_id, repo_id }
    "consultation.result",      # data: { task_id, agent_id, confidence, gap_count }
    "synthesis.started",        # data: { task_id, consultation_count }
    "task.delivered",           # data: TaskResult (full)
]


class TaskCreatedEvent(BaseModel):
    task_id: str
    status: Literal["received"]


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
    """Full TaskResult payload -- shape defined by the orchestration layer."""
    task_id: str
    result: dict  # full TaskResult
```

```python
# bifrost/api/routes/streaming.py

from collections.abc import AsyncGenerator
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from bifrost.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/tasks", tags=["streaming"])


@router.get("/{task_id}/stream")
async def stream_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Server-Sent Events stream for task progress.
    Content-Type: text/event-stream.
    Each SSE frame: `event: {type}\\ndata: {JSON}\\n\\n`"""

    async def event_generator() -> AsyncGenerator[str, None]:
        async for event in task_coordinator.stream_events(task_id):
            yield f"event: {event.event_type}\ndata: {event.model_dump_json()}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
```

### 4.5 Sessions endpoints: not built

### 4.6 Feedback Endpoint

```python
# bifrost/api/schemas/feedback.py

from pydantic import BaseModel


class CorrectionEntry(BaseModel):
    entity_id: str | None = None
    original_claim: str
    corrected_claim: str
    correction_type: str                        # CorrectionType enum value


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
```

```python
# bifrost/api/routes/feedback.py

from fastapi import APIRouter, Depends, status
from bifrost.api.deps import get_current_user
from bifrost.api.schemas.envelope import BifrostEnvelope
from bifrost.api.schemas.feedback import SubmitFeedbackRequest, SubmitFeedbackData

router = APIRouter(prefix="/api/v1/feedback", tags=["feedback"])


@router.post(
    "",
    response_model=BifrostEnvelope[SubmitFeedbackData],
    status_code=status.HTTP_201_CREATED,
)
async def submit_feedback(
    body: SubmitFeedbackRequest,
    current_user: User = Depends(get_current_user),
) -> BifrostEnvelope[SubmitFeedbackData]:
    """Submit feedback on a completed task.
    Errors: 400 (ValidationError), 404 (task not found)"""
    ...
```

### 4.7 Health Endpoint

```python
# bifrost/api/schemas/health.py

from pydantic import BaseModel


class CheckStatus(BaseModel):
    status: str
    latency_ms: float


class HealthChecks(BaseModel):
    database: CheckStatus
    redis: CheckStatus
    adapters: dict[str, CheckStatus]


class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded", "unhealthy"]
    version: str
    track: Literal["A", "B"]
    checks: HealthChecks
    uptime_seconds: float
```

```python
# bifrost/api/routes/health.py

from fastapi import APIRouter
from bifrost.api.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Instance health check (no auth required).
    HTTP 200 for healthy/degraded, 503 for unhealthy."""
    ...
```


---

## 5. Error Taxonomy

All errors extend the base exception class. HTTP status codes are deterministic from error type. FastAPI exception handlers translate exceptions to JSON responses automatically.

### 5.1 Error Codes

```python
# bifrost/errors.py

from enum import StrEnum


class BifrostErrorCode(StrEnum):
    """Canonical error codes returned in all error responses."""
    EXTRACTION_ERROR = "EXTRACTION_ERROR"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    NOT_IMPLEMENTED_TRACK_B = "NOT_IMPLEMENTED_TRACK_B"
    CONFLICT = "CONFLICT"
    ADAPTER_ERROR = "ADAPTER_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    AUTH_FAILURE = "AUTH_FAILURE"


class ExtractionPhase(StrEnum):
    """Phases within the extraction pipeline where failures can occur."""
    CLONE = "clone"
    TRIAGE = "triage"
    COMPONENT = "component"
    FLOW = "flow"
    DEPENDENCY = "dependency"
    WRITE = "write"


class BudgetDimension(StrEnum):
    """Dimensions that can trigger a budget exceeded error."""
    COST = "cost"
    LLM_CALLS = "llm_calls"
    WALL_TIME = "wall_time"
```

### 5.2 Exception Classes

```python
# bifrost/errors.py (continued)

from __future__ import annotations

from typing import Any


class BifrostError(Exception):
    """
    Base class for all Bifrost errors. All subclasses must set code, status_code,
    and retryable at construction time.
    """

    def __init__(
        self,
        message: str,
        *,
        code: BifrostErrorCode,
        status_code: int,
        retryable: bool,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.detail = detail


class ExtractionError(BifrostError):
    """
    Thrown during extraction pipeline phases (clone, triage, component, flow,
    dependency, write). The phase field identifies where in the pipeline the
    failure occurred.
    """

    def __init__(
        self,
        message: str,
        *,
        repo_id: str,
        phase: ExtractionPhase,
        retryable: bool = False,
        detail: Any = None,
    ) -> None:
        super().__init__(
            message,
            code=BifrostErrorCode.EXTRACTION_ERROR,
            status_code=500,
            retryable=retryable,
            detail=detail,
        )
        self.repo_id = repo_id
        self.phase = phase


class BudgetExceededError(BifrostError):
    """
    Thrown when a task or extraction run exceeds its LLM cost budget.
    The orchestrator catches this and returns a partial result, not a 5xx.
    """

    def __init__(
        self,
        *,
        used: float,
        limit: float,
        currency: str = "usd",
        dimension: BudgetDimension,
    ) -> None:
        super().__init__(
            f"Budget exceeded: {dimension} used={used} limit={limit}",
            code=BifrostErrorCode.BUDGET_EXCEEDED,
            status_code=402,
            retryable=False,
        )
        self.used = used
        self.limit = limit
        self.currency = currency
        self.dimension = dimension


class NotImplementedTrackBError(BifrostError):
    """
    Thrown when a caller requests a Track B feature while running Track A.
    All Track B stubs raise this so the orchestrator can surface a clear message.
    """

    track_required: str = "B"
    access_granted: bool = False

    def __init__(self, feature_name: str) -> None:
        super().__init__(
            f"{feature_name} requires Track B (Managed Agents). "
            "Set BIFROST_TRACK=B and ensure Research Preview access.",
            code=BifrostErrorCode.NOT_IMPLEMENTED_TRACK_B,
            status_code=501,
            retryable=False,
        )
        self.feature_name = feature_name


class ConflictError(BifrostError):
    """
    Thrown by AgentStateBackend.write() when a precondition fails.
    Callers must re-read the current document and retry the write.
    Used for optimistic concurrency in knowledge_text_cache.
    """

    def __init__(
        self,
        path: str,
        *,
        current_hash: str,
        expected_hash: str,
    ) -> None:
        super().__init__(
            f"Optimistic concurrency conflict at path: {path}",
            code=BifrostErrorCode.CONFLICT,
            status_code=409,
            retryable=True,  # caller should re-read and retry
        )
        self.path = path
        self.current_hash = current_hash
        self.expected_hash = expected_hash


class AdapterError(BifrostError):
    """
    Thrown by SourceAdapter implementations after all retry attempts are
    exhausted. The adapter_id field identifies which adapter failed (for
    routing diagnostics).
    """

    def __init__(
        self,
        message: str,
        *,
        adapter_id: str,
        adapter_type: str,
        retryable: bool = False,
        code: BifrostErrorCode = BifrostErrorCode.ADAPTER_ERROR,
        detail: Any = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            status_code=502,
            retryable=retryable,
            detail=detail,
        )
        self.adapter_id = adapter_id
        self.adapter_type = adapter_type


class ValidationError(BifrostError):
    """
    Thrown when a request body or LLM JSON output fails Pydantic validation.
    The field property identifies the specific failing path for client feedback.
    """

    def __init__(
        self,
        message: str,
        *,
        pydantic_errors: list[dict[str, Any]],
        field: str,
    ) -> None:
        super().__init__(
            message,
            code=BifrostErrorCode.VALIDATION_ERROR,
            status_code=400,
            retryable=False,
            detail=pydantic_errors,
        )
        self.pydantic_errors = pydantic_errors
        self.field = field
```

### 5.3 FastAPI Exception Handlers

```python
# bifrost/api/error_handlers.py

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bifrost.errors import (
    AdapterError,
    BifrostError,
    BudgetExceededError,
    ConflictError,
    ExtractionError,
    NotImplementedTrackBError,
    ValidationError,
)


def register_exception_handlers(app: FastAPI) -> None:
    """Register all Bifrost exception handlers on the FastAPI app."""

    @app.exception_handler(ValidationError)
    async def handle_validation_error(
        request: Request, exc: ValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": exc.retryable,
                    "field": exc.field,
                    "detail": exc.detail,
                }
            },
        )

    @app.exception_handler(BudgetExceededError)
    async def handle_budget_exceeded(
        request: Request, exc: BudgetExceededError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": exc.retryable,
                    "used": exc.used,
                    "limit": exc.limit,
                    "dimension": exc.dimension,
                }
            },
        )

    @app.exception_handler(ConflictError)
    async def handle_conflict_error(
        request: Request, exc: ConflictError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": exc.retryable,
                    "current_hash": exc.current_hash,
                    "expected_hash": exc.expected_hash,
                }
            },
        )

    @app.exception_handler(NotImplementedTrackBError)
    async def handle_not_implemented(
        request: Request, exc: NotImplementedTrackBError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": exc.retryable,
                    "track_required": exc.track_required,
                }
            },
        )

    @app.exception_handler(AdapterError)
    async def handle_adapter_error(
        request: Request, exc: AdapterError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": exc.retryable,
                    "adapter_id": exc.adapter_id,
                    "adapter_type": exc.adapter_type,
                }
            },
        )

    @app.exception_handler(ExtractionError)
    async def handle_extraction_error(
        request: Request, exc: ExtractionError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": exc.retryable,
                    "repo_id": exc.repo_id,
                    "phase": exc.phase,
                }
            },
        )

    @app.exception_handler(BifrostError)
    async def handle_bifrost_error(
        request: Request, exc: BifrostError
    ) -> JSONResponse:
        """Catch-all for any BifrostError subclass without a specific handler."""
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": exc.retryable,
                    "detail": exc.detail,
                }
            },
        )
```

### 5.4 HTTP Status Mapping

| Exception Class | HTTP Status | Retryable by Orchestrator |
|---|---|---|
| `ValidationError` | 400 Bad Request | No |
| Auth failure (middleware) | 401 Unauthorized | No |
| `BudgetExceededError` | 402 (budget exceeded) | No (returns partial) |
| `BifrostError` (generic not-found) | 404 Not Found | No |
| `ConflictError` | 409 Conflict | Yes (re-read + retry) |
| `ExtractionError` (recoverable) | 500 Internal Server Error | Yes (once) |
| `ExtractionError` (non-recoverable) | 500 Internal Server Error | No |
| `NotImplementedTrackBError` | 501 Not Implemented | No |
| `AdapterError` (rate limited) | 502 Bad Gateway | Yes (up to 3x, 1s/2s/4s) |

### 5.5 Retry Eligibility

The orchestrator's `with_retry` wrapper checks `exc.retryable` before deciding to retry. Retry budget is fixed at 3 attempts with 1s/2s/4s exponential backoff. The orchestrator MUST NOT retry `BudgetExceededError` or `ValidationError` -- these indicate caller errors, not transient failures.

```python
# bifrost/errors.py (`with_retry`)

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from bifrost.errors import BifrostError

T = TypeVar("T")

RETRY_BACKOFF_SECONDS: list[float] = [1.0, 2.0, 4.0]


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
) -> T:
    """
    Execute an async callable with exponential backoff retry.
    Only retries when the raised BifrostError has retryable=True.
    Non-BifrostError exceptions propagate immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except BifrostError as exc:
            last_exc = exc
            if not exc.retryable or attempt >= max_attempts - 1:
                raise
            await asyncio.sleep(RETRY_BACKOFF_SECONDS[attempt])
        except Exception:
            raise
    # Unreachable, but satisfies type checker
    assert last_exc is not None
    raise last_exc
```


---

## 6. Testing Strategy

See [CONTRIBUTING.md](../../CONTRIBUTING.md) for the test-first rule and the "Run the tests" section of [RUNNING.md](../../RUNNING.md#9-run-the-tests) for the commands.

---

## 7. Performance Requirements

These are design targets, not measured results.

All latency targets are measured end-to-end from client request to final result delivery.

### 7.1 Query Latency SLA

| Task Type | p50 | p95 | p99 |
|---|---|---|---|
| Cache hit (fresh T1/T2, single-repo) | < 1s | < 2s | < 5s |
| Simple multi-agent (3 RepoAgents, fresh knowledge) | < 5s | < 12s | < 20s |
| Complex multi-agent (5 RepoAgents, some stale) | < 10s | < 20s | < 30s |
| Incremental extraction triggered during query | < 60s | < 2 min | < 5 min |
| Cold start extraction (small repo, < 200 files) | < 5 min | < 12 min | < 20 min |
| Cold start extraction (large repo, 200-500 files) | < 10 min | < 20 min | < 30 min |

### 7.2 Track A vs Track B Latency Comparison

| Scenario | Track A p95 | Track B p95 | Delta |
|---|---|---|---|
| Cache hit | < 2s | < 3s | +1s (session init overhead) |
| Simple multi-agent | < 12s | < 15s | +3s (session spinup) |
| Complex multi-agent | < 20s | < 25s | +5s (session + MCP overhead) |

Track B adds ~$0.03-0.05 per query in session overhead (tracked in `llm_cost_log.track_b_session_overhead_usd`).

### 7.3 Extraction Throughput

| Metric | Target |
|---|---|
| Files triaged per minute (Haiku triage phase) | >= 60 files/min |
| Components extracted per minute (Sonnet analysis) | >= 10 components/min |
| Checkpoint write latency | < 200ms |
| Knowledge index build latency (<= 100 entities) | < 5s |

**Runtime notes:** Track A runs on Python 3.12+ asyncio event loop via uvicorn. The extraction pipeline uses `async for` generators to stream file batches through triage and analysis phases. asyncpg provides native PostgreSQL async I/O -- checkpoint writes and entity upserts are non-blocking. CPU-bound operations (e.g., large dependency graph computation) use `asyncio.to_thread()` to avoid blocking the event loop.

Extraction concurrency is controlled by `asyncio.Semaphore` -- the triage phase runs up to 10 concurrent Haiku calls, while the analysis phase limits to 3 concurrent Sonnet calls to respect rate limits and cost budgets.

### 7.4 Concurrent Queries

| Metric | Target |
|---|---|
| Simultaneous task executions without degradation | 10 (Track A) |
| Track B max concurrent sessions | 3 (Research Preview limit) |
| Redis cache hit rate target (warm system) | >= 70% |
| Acceptable p95 DB query latency under load | < 100ms |

**Python asyncio notes:** uvicorn runs a single-process asyncio event loop by default. For production concurrency targets (10 simultaneous tasks), deploy with `uvicorn --workers 2` or use gunicorn with `UvicornWorker`. Each worker maintains its own asyncpg connection pool (`DB_POOL_MIN` to `DB_POOL_MAX` connections). The asyncpg driver's native connection pooling is preferred over SQLAlchemy's `QueuePool` for async workloads.

---

## 8. Security Implementation Checklist

This is an implementer checklist. Each item maps to a specific spec section. Mark complete before PR merge.

### Request Handling

- [ ] All request bodies validated with Pydantic models before any business logic executes (FastAPI dependency injection, not per-handler)
- [ ] A principal is resolved for every non-public route by `IdentityMiddleware` (see the auth paragraph in §4)
- [ ] No raw path/query params passed directly to DB queries -- always parameterized via SQLAlchemy ORM or bound parameters (`:param` with `text()`)
- [ ] Request size limit enforced by uvicorn/nginx (`--limit-request-body`) -- 1MB max for API requests

### Adapter Security

- [ ] `validate_adapter_url()` called before any adapter `base_url` is used -- blocks RFC 1918 ranges, loopback, metadata endpoints (`169.254.169.254`)
- [ ] `validate_adapter_url()` blocks schemes other than `https://` in production (`BIFROST_ENV != "development"`)
- [ ] Adapter credentials stored in environment variables only -- never in PostgreSQL plaintext or JSONB columns

### Content Security

- [ ] `scan_for_secrets()` called on all `ExtractedContent.content` before it enters LLM context -- AWS keys, GitHub tokens, private keys, connection strings
- [ ] `inject_canary_token()` called in all system prompts that include source code -- token format: `BIFROST_INTEGRITY_TOKEN: {random-per-session-UUID}`
- [ ] Content size validated before LLM submission: `token_estimate <= 50_000` or `AdapterContentPolicy.overflow_strategy` applied
- [ ] RESTRICTED repos are processed only when your Claude plan's data terms allow it (from S11) -- `repo.classification == "RESTRICTED"` uses the same runtime as other repos

### LLM Output Validation

- [ ] `validate_pydantic_response()` called on all structured LLM JSON outputs (component extraction, flow extraction, dependency extraction)
- [ ] LLM responses that fail Pydantic validation are logged to `agent_session_events` with `event_type='validation_error'` before being discarded
- [ ] Confidence level from LLM output clamped to `ConfidenceLevel` enum -- invalid values default to `'uncertain'`

### Storage Access Control

- [ ] `ScopedStorageClient` used for all Track A entity reads in `agents/` package (enforces `scope_repo_id` filter on all queries)
- [ ] `ScopedMemoryStoreClient` used for all Track B writes (enforces path prefix -- agent cannot write outside `/repo/{its-repo-id}/`)
- [ ] `knowledge_text_cache` writes include content_hash for optimistic concurrency -- raw overwrites without precondition are forbidden

### Track B Specific

- [ ] `TRACK_B_DPA_CONFIRMED=true` env var checked at startup before any Track B session is created
- [ ] Session credentials injected via session config only -- NEVER in system prompt or user message
- [ ] Credential values never logged to `llm_cost_log`, `agent_session_events`, or `task_consultation_log` -- only opaque `credential_id`

### Dependency & Runtime Security

- [ ] `pip-audit` or `uv audit` runs in CI on every PR -- fails build on known vulnerabilities
- [ ] FastAPI CORS middleware configured with explicit `allow_origins` (no wildcard `*` in production)
- [ ] `TrustedHostMiddleware` configured to reject requests with unexpected `Host` headers
- [ ] Python-specific secret patterns added to `scan_for_secrets()`: Django secret keys, `.pypirc` tokens, `SQLALCHEMY_DATABASE_URI` leaks
- [ ] No `eval()`, `exec()`, or `pickle.loads()` on untrusted input anywhere in codebase

---

## 9. Configuration Reference

The environment variables and settings are listed in `.env.example` and in [handbook 10](../handbook/10-operations.md). The `BIFROST_*` names that were planned here are not the built names.

---

## 10. Implementation patterns

Superseded by the handbook in `docs/handbook/`. The patterns that were sketched here are described there as built.
