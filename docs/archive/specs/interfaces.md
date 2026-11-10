# Bifrost Interface Specification

**Spec ID:** S10
**Status:** Draft

---

## 1. Purpose and Scope

This specification defines the three external surfaces through which users and agents interact with Bifrost:

1. **REST API** -- the single source of truth for all platform operations.
2. **CLI** -- a developer-facing command-line tool that calls the REST API.
3. **Control Plane UI** -- a browser-based dashboard for visibility, administration, and task management.

Every operation that the CLI or UI can perform is backed by an API call. There are no "UI-only" or "CLI-only" code paths; the API is authoritative. This document covers the complete operation catalog, the standard response envelope, endpoint design, CLI command tree, and UI page structure.

---

## 2. Glossary References

| Term | Definition |
|------|-----------|
| **Task** | An asynchronous unit of work submitted to Bifrost (a question, trace, impact analysis, etc.). |
| **Specialist / Agent** | A scoped AI agent responsible for a repo, source, domain, or coordination role. |
| **Flow** | An end-to-end path of execution across one or more repos and services. |
| **Freshness** | A measure of how current a knowledge artifact is relative to its upstream source state. |
| **Lag** | The delta between the latest known source state and the latest indexed/derived knowledge state. |
| **Confidence** | An explicit annotation on every answer indicating how well-grounded it is. |
| **Evidence** | A reference to a specific source object (file, commit, ticket, trace, etc.) that supports a finding. |
| **Domain** | A logical grouping of related repos that together serve a business capability. |

---

## 3. Requirements Traceability

This specification addresses the following requirements sections from `docs/prd.md`:

| Requirement Section | Coverage |
|---|---|
| S18 -- UI requirements | Control Plane UI (section 9) |
| S18.1 -- Central control plane UI | Dashboard page, global search, task submission |
| S18.2 -- Repo UI | Repo View page |
| S18.3 -- Domain UI | Domain View page |
| S18.4 -- Task UI | Task View page |
| S18.5 -- User refresh controls | Refresh endpoints, CLI `refresh` command, Admin page |
| S19 -- CLI and API requirements | API specification (section 7), CLI specification (section 8) |
| S19.1 -- CLI requirements | CLI command tree |
| S19.2 -- API requirements | REST API endpoints |
| S3 -- Product outcomes | Capability Catalog maps 1:1 to outcome categories |
| S4 -- User requirements | Human users served by all three surfaces; AI users served by API |
| S12 -- Freshness | Freshness metadata in every response envelope |
| S16 -- Confidence, uncertainty, conflict | Confidence, gaps, conflicts in every response envelope |
| S20 -- Logging and audit | Task logs via GET /tasks/:id, audit trail in response metadata |
| S21 -- Security and governance | Auth model, permission-aware access |

---

## 4. Design Goals

### 4.1 API-First

The REST API is the single source of truth for all functionality. The CLI and the Control Plane UI are both consumers of the same API. This guarantees:

- Feature parity across all surfaces.
- A single place to enforce authorization, rate limiting, and audit logging.
- AI agents and external integrations have full access to the same capabilities as human users.

### 4.2 Consistent Response Envelope

Every API response, regardless of endpoint, uses the same envelope structure. The CLI and UI unpack this envelope and present it in surface-appropriate ways, but the underlying data contract is identical.

### 4.3 Async-First for Heavy Operations

Operations that involve agent consultation (understand, trace, impact, plan, similar, generate-doc) are submitted as asynchronous tasks. Lightweight read operations (list repos, get freshness, get task status) are synchronous.

### 4.4 Streaming for Progress

Long-running tasks expose an SSE stream for real-time progress updates. Both the CLI and UI consume this stream.

---

## 5. Capability Catalog

Every operation Bifrost supports is listed here. Each operation maps to at least one API endpoint and at least one CLI command.

| Operation | Description | Category |
|---|---|---|
| `understand-repo` | Get comprehensive repo understanding: purpose, modules, interfaces, patterns, freshness | Understand |
| `understand-domain` | Get domain-level understanding: repos, cross-repo flows, shared patterns, stale areas | Understand |
| `understand-concept` | Explain a concept or keyword in the context of a repo, domain, or org | Understand |
| `trace-flow` | Trace an end-to-end flow by keyword, entry point, or concept | Understand |
| `understand-change` | Understand a specific change (commit, PR, branch diff) in context | Understand |
| `assess-impact` | Blast radius analysis for a proposed or actual change | Validate |
| `find-dependencies` | Direct and indirect dependency analysis at repo, domain, or org level | Validate |
| `validate-pattern` | Check whether proposed work conflicts with existing patterns or conventions | Validate |
| `find-similar` | Find similar implementations, patterns, or reusable approaches | Reuse |
| `classify-work` | Classify proposed work as pattern-extension, variation, net-new, or conflict | Reuse |
| `plan-change` | Confidence-aware plan for a feature, bug fix, migration step, or refactor | Plan |
| `compare-implementations` | Compare how different repos or domains implement the same concept | Plan |
| `generate-doc` | Generate or refresh documentation for a specified scope | Document |
| `get-doc-status` | Get publication state, freshness, and staleness for docs in a scope | Document |
| `check-freshness` | Check freshness and lag for a repo, domain, source, or org slice | Monitor |
| `request-refresh` | Trigger a knowledge refresh at a specified scope | Monitor |
| `list-repos` | List registered repos with summary metadata | Admin |
| `get-repo` | Get detailed repo information including sources, freshness, patterns | Admin |
| `list-domains` | List configured domains with summary metadata | Admin |
| `get-domain` | Get detailed domain information | Admin |
| `list-agents` | List active agents/specialists and their health | Admin |
| `get-agent` | Get detailed agent info: scope, sources, freshness, recent consultations | Admin |
| `submit-task` | Submit an async task (question, trace, impact, plan, etc.) | Task |
| `get-task` | Get task status, progress, and results | Task |
| `list-tasks` | List recent tasks with filtering | Task |
| `cancel-task` | Cancel a running task | Task |
| `stream-task` | Stream real-time progress for a running task | Task |
| `search` | Global search across repos, flows, concepts, docs | Search |
| `submit-feedback` | Submit quality signal, gap reports, and corrections on a completed task | Feedback |
| `get-feedback` | Retrieve all feedback submitted for a given task | Feedback |

---

## 6. Standard Response Envelope

Every API response is wrapped in a consistent envelope. This is the single contract that all consumers (CLI, UI, external agents) depend on.

```python
from enum import StrEnum
from typing import Generic, TypeVar
from pydantic import BaseModel
from datetime import datetime

T = TypeVar("T")

class DataSource(StrEnum):
    """Whether response was served from AgentStateBackend cache or fresh agent analysis."""
    CACHED = "cached"
    FRESH = "fresh"
    PARTIAL = "partial"   # some repos cached, some fresh

class ConfidenceLevel(StrEnum):
    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    STALE = "stale"
    UNCERTAIN = "uncertain"
    UNKNOWN = "unknown"
    CONFLICTING = "conflicting"

class FreshnessDetail(BaseModel):
    scope: str                # e.g. "repo:webhook-api", "source:github:webhook-api"
    last_indexed: datetime    # ISO 8601
    last_source_change: datetime  # ISO 8601
    lag_seconds: int
    state: str                # "fresh" | "slightly_stale" | "stale" | "very_stale" | "unknown"

class FreshnessState(BaseModel):
    overall: str              # "fresh" | "slightly_stale" | "stale" | "very_stale" | "unknown"
    details: list[FreshnessDetail]

class EvidenceRef(BaseModel):
    source_type: str          # "code" | "commit" | "pr" | "ticket" | "doc" | "config" | "trace" | "metric" | ...
    source_id: str            # unique identifier within source system
    location: str             # human-readable location (file path, URL, etc.)
    relevance: str            # "primary" | "supporting" | "context"
    snippet: str | None = None  # optional short excerpt

class Gap(BaseModel):
    description: str
    scope: str
    severity: str             # "critical" | "significant" | "minor"
    suggestion: str | None = None  # what could resolve the gap

class ConflictType(StrEnum):
    SOURCE_CONFLICT = "source_conflict"
    STALE_VS_CURRENT = "stale_vs_current"
    RUNTIME_VS_STATIC = "runtime_vs_static"
    INTENT_VS_IMPLEMENTATION = "intent_vs_implementation"
    DEPENDENCY_DISAGREEMENT = "dependency_disagreement"
    OWNERSHIP_DISAGREEMENT = "ownership_disagreement"

class ConflictSide(BaseModel):
    source: str
    claim: str
    evidence: list[EvidenceRef]
    confidence: ConfidenceLevel

class Conflict(BaseModel):
    type: ConflictType
    description: str
    sides: list[ConflictSide]
    resolution: str | None = None

class CacheStatus(StrEnum):
    LIVE = "live"
    CACHED_FRESH = "cached_fresh"
    CACHED_STALE = "cached_stale"
    PARTIAL_CACHE = "partial_cache"
    NO_CACHE = "no_cache"

class ResponseMetadata(BaseModel):
    confidence: ConfidenceLevel
    freshness: FreshnessState
    evidence_summary: list[EvidenceRef]
    gaps: list[Gap]
    conflicts: list[Conflict]
    cache_status: CacheStatus
    data_source: DataSource    # storage transparency: where this response came from
    task_id: str
    duration_ms: int

class BifrostResponse(BaseModel, Generic[T]):
    data: T
    metadata: ResponseMetadata
```

### 6.1 Envelope for Synchronous Responses

Synchronous endpoints (list, get, freshness) return the envelope directly in the HTTP response body.

### 6.2 Envelope for Asynchronous Responses

Async endpoints (task submission) return an immediate acknowledgment:

```json
{
  "data": {
    "task_id": "tsk_01HXYZ...",
    "status": "received",
    "agent_runtime": "A",
    "stream_url": "/api/v1/tasks/tsk_01HXYZ.../stream",
    "estimated_duration_ms": 12000,
    "estimated_agents": 3,
    "estimated_repos": ["dispatcher", "signing-keys", "usage-metering"]
  },
  "metadata": {
    "confidence": "unknown",
    "freshness": { "overall": "unknown", "details": [] },
    "evidence_summary": [],
    "gaps": [],
    "conflicts": [],
    "cache_status": "no_cache",
    "data_source": "fresh",
    "task_id": "tsk_01HXYZ...",
    "duration_ms": 45
  }
}
```

The full envelope (with populated confidence, evidence, gaps, conflicts) is available when the task completes, retrieved via `GET /api/v1/tasks/:id`.

---

## 7. API Specification

### 7.1 Overview

| Property | Value |
|---|---|
| Style | REST (JSON over HTTP) |
| Base URL | `/api/v1/` |
| Auth | Resolved per request by `identity.provider`; scripts use `Bearer bft_…` tokens (see 7.2) |
| Content type | `application/json` |
| Optional | GraphQL endpoint for flexible knowledge graph queries (deferred to post-V1) |

### 7.2 Authentication

Bifrost resolves a principal for every non-public route in `IdentityMiddleware` (`src/bifrost/api/auth/`). `identity.provider` chooses how: `none` treats a loopback caller as the admin operator and needs no header; `header` reads identity from trusted proxy headers; `oidc_jwt` verifies an `Authorization: Bearer` JWT against the configured JWKS and accepts RS256 and ES256 only. Scripts can send `Authorization: Bearer bft_…` tokens minted with `bifrost token new`, which Bifrost matches by sha256 against `api_tokens` in `bifrost.yaml`. Any other bearer value gets 401 `invalid_credentials`, a request with no principal gets 401 `identity_required`, and a principal without the needed role gets 403 `auth.role_required`. The only auth routes are `GET /api/v1/auth/me`, `POST /api/v1/auth/refresh` and `POST /api/v1/auth/logout`. See [RUNNING.md section 6](../../../RUNNING.md#6-identity) and [handbook 02 §8.1](../../handbook/02-architecture.md#81-auth-at-the-api-boundary).

### 7.3 Endpoint Reference

#### 7.3.1 Tasks

| Method | Path | Description | Async |
|---|---|---|---|
| `POST` | `/api/v1/tasks` | Submit a new task | Yes |
| `GET` | `/api/v1/tasks` | List tasks (paginated, filterable) | No |
| `GET` | `/api/v1/tasks/:id` | Get task status and result | No |
| `DELETE` | `/api/v1/tasks/:id` | Cancel a running task | No |
| `GET (SSE)` | `/api/v1/tasks/:id/stream` | Stream task progress via SSE | N/A |

**POST /api/v1/tasks -- Submit a task**

```json
// Request
{
  "operation": "trace-flow",
  "params": {
    "keyword": "webhook delivery",
    "scope": { "domain": "delivery" },
    "include_runtime": true
  },
  "options": {
    "include_local": false,
    "max_depth": 3,
    "output_detail": "full"
  }
}

// Response (202 Accepted)
{
  "data": {
    "task_id": "tsk_01HXYZ...",
    "operation": "trace-flow",
    "status": "received",
    "agent_runtime": "A",
    "stream_url": "/api/v1/tasks/tsk_01HXYZ.../stream",
    "estimated_duration_ms": 15000,
    "estimated_agents": 3,
    "estimated_repos": ["dispatcher", "signing-keys", "usage-metering"]
  },
  "metadata": { ... }
}
```

**GET /api/v1/tasks/:id -- Get task result**

```json
// Response (200 OK, when completed)
{
  "data": {
    "task_id": "tsk_01HXYZ...",
    "operation": "trace-flow",
    "status": "completed",
    "result": {
      "flow": {
        "name": "Webhook Delivery",
        "entry_point": { "repo": "web-frontend", "path": "src/webhooks/trigger.ts" },
        "segments": [ ... ],
        "dependencies": [ ... ]
      }
    },
    "agents_consulted": ["agent:webhook-api", "agent:web-frontend", "agent:domain:delivery"],
    "started_at": "2025-01-15T10:00:00Z",
    "completed_at": "2025-01-15T10:00:14Z"
  },
  "metadata": {
    "confidence": "confirmed",
    "freshness": {
      "overall": "fresh",
      "details": [
        { "scope": "repo:webhook-api", "last_indexed": "2025-01-15T09:30:00Z", "last_source_change": "2025-01-15T09:28:00Z", "lag_seconds": 120, "state": "fresh" },
        { "scope": "repo:web-frontend", "last_indexed": "2025-01-15T08:00:00Z", "last_source_change": "2025-01-15T09:45:00Z", "lag_seconds": 6300, "state": "slightly_stale" }
      ]
    },
    "evidence_summary": [
      { "source_type": "code", "source_id": "webhook-api:src/handlers/deliver.ts", "location": "webhook-api/src/handlers/deliver.ts:42", "relevance": "primary" },
      { "source_type": "trace", "source_id": "trace:abc123", "location": "Datadog trace abc123", "relevance": "supporting" }
    ],
    "gaps": [
      { "description": "Retry behavior after delivery-gateway timeout is not covered by any source", "scope": "repo:webhook-api", "severity": "significant" }
    ],
    "conflicts": [],
    "cache_status": "partial_cache",
    "task_id": "tsk_01HXYZ...",
    "duration_ms": 14200
  }
}
```

**GET (SSE) /api/v1/tasks/:id/stream -- Stream progress**

See [Streaming Architecture](#streaming-architecture) section for full SSE event definitions and dual-track implementation details.

```
event: task.started
data: {"task_id":"tsk_01HXYZ...","type":"trace-flow","scope":{"domain":"delivery"}}

event: agent.consulting
data: {"repo":"webhook-api","status":"analyzing"}

event: agent.found
data: {"repo":"webhook-api","finding_summary":"Replay handler at src/handlers/replay.ts","confidence":"confirmed"}

event: text.delta
data: {"content":"The replay flow starts at...","index":0}

event: task.complete
data: {"task_id":"tsk_01HXYZ...","confidence":"confirmed","gaps":[],"freshness":{"overall":"fresh","details":[]}}

event: task.error
data: {"task_id":"tsk_01HXYZ...","error":"agent timeout","partial_result":{}}
```

---

## Streaming Architecture

Bifrost streams task progress and results to clients via Server-Sent Events (SSE).
The client-facing SSE API is identical in both tracks. Internal implementation differs.

### Client-Facing SSE Endpoint

```
GET /api/v1/tasks/{task_id}/stream
  Content-Type: text/event-stream
  Authorization: Bearer bft_<token>   (not needed when the identity provider supplies the principal)
```

FastAPI route implementation pattern:

```python
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/v1")

@router.get("/tasks/{task_id}/stream")
async def stream_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    async def event_generator() -> AsyncGenerator[str, None]:
        async for event in task_coordinator.execute(task_id):
            yield f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
```

### SSE Event Types (same in both tracks)

```
event: task.started
data: {"task_id": "...", "type": "trace-flow", "scope": {...}}

event: agent.consulting
data: {"repo": "dispatcher", "status": "analyzing"}

event: agent.found
data: {"repo": "dispatcher", "finding_summary": "...", "confidence": "confirmed"}

event: text.delta
data: {"content": "The delivery flow starts at...", "index": 0}

event: task.complete
data: {"task_id": "...", "confidence": "inferred", "gaps": [...], "freshness": {...}}

event: task.error
data: {"task_id": "...", "error": "...", "partial_result": {...}}

event: session.timeout
data: {"session_id": "...", "agent_type": "...", "checkpoint_path": "...", "respawn_attempt": 0}
```

Pydantic model for the `session.timeout` event:

```python
class SessionTimeoutEvent(BaseModel):
    session_id: str
    agent_type: str
    checkpoint_path: str
    respawn_attempt: int
```

This event is emitted by Track B only, when a Managed Agent session hits its duration limit. The `checkpoint_path` points to the Memory Store location where the session state was saved before timeout. `respawn_attempt` is 0 for the first timeout, incrementing on each subsequent respawn.

### Track A Internal Implementation

Track A is the V1 implementation: Python `AsyncGenerator`-based streaming inside the FastAPI server, returned via `StreamingResponse`.

1. Client calls `GET /api/v1/tasks/{id}/stream`
2. FastAPI route creates a `TaskCoordinator` instance
3. `TaskCoordinator.execute(task)` returns `AsyncGenerator[TaskEvent, None]`
4. A FastAPI `StreamingResponse` wraps the generator, formatting `TaskEvent`s as SSE `text/event-stream` data
5. No external SSE server — pure ASGI response stream via `StreamingResponse(event_generator(), media_type="text/event-stream")`

### Track B Internal Implementation

Track B is the future implementation: Bifrost proxies an Anthropic Managed Agents session event stream to users.

1. Client calls `GET /api/v1/tasks/{id}/stream`
2. Bifrost creates a Managed Agent Session (`POST /v1/sessions`)
3. Bifrost subscribes to the session stream (`GET /v1/sessions/{id}/stream`)
4. Bifrost maps session events to Bifrost SSE format:
   - `session.thread_created` → `agent.consulting` event
   - `session.thread_idle` → `agent.found` event
   - `agent.message` (text) → `text.delta` event
   - `session.complete` → `task.complete` event
5. Bifrost proxies transformed events to the client stream

The client-facing API contract (endpoint, event types, event payloads) is identical in both tracks. Track B is an internal implementation change with no client-facing API changes.

#### 7.3.1.1 Session Management (Track B only)

Sessions endpoints: not built.

#### Track B SSE Implementation

Track A: TaskCoordinator yields events directly to the SSE connection via an in-process `asyncio.Queue`. No polling; events are zero-latency relative to when they are generated.

Track B: TaskCoordinator polls Memory Store for state changes (path: `/task/{task_id}/events/*.md`) and forwards new entries as SSE events. Polling intervals:

- **Active session**: 500ms
- **Session startup / teardown**: 2s

This means Track B SSE events have ~500ms additional latency compared to Track A during normal operation. Clients must tolerate this latency; the SSE protocol itself is unchanged.

Because Track B polls a shared Memory Store rather than holding a direct session connection, it naturally handles session respawns: when a session times out and is respawned, the new session continues writing events to the same Memory Store path, and the polling loop forwards them without the client observing any connection interruption (other than a `session.timeout` event followed by a new `agent.consulting` event).

---

#### 7.3.2 Repos

| Method | Path | Description | Async |
|---|---|---|---|
| `GET` | `/api/v1/repos` | List registered repos | No |
| `POST` | `/api/v1/repos` | Register a new repo | No |
| `GET` | `/api/v1/repos/:id` | Repo detail with freshness, sources, agents | No |
| `DELETE` | `/api/v1/repos/:id` | Deregister a repo (soft delete) | No |
| `GET` | `/api/v1/repos/:id/status` | Repo index status and cost estimate | No |
| `POST` | `/api/v1/repos/:id/extract` | Trigger manual extraction | Yes |
| `GET` | `/api/v1/repos/:id/flows` | Flows touching this repo | No |
| `GET` | `/api/v1/repos/:id/dependencies` | Dependency graph for this repo | No |
| `GET` | `/api/v1/repos/:id/patterns` | Learned patterns for this repo | No |
| `GET` | `/api/v1/repos/:id/docs` | Docs status for this repo | No |

**GET /api/v1/repos -- List repos**

```json
// Response (200 OK)
{
  "data": {
    "items": [
      {
        "id": "repo_webhook-api",
        "name": "webhook-api",
        "domain": "delivery",
        "freshness": "fresh",
        "agent_count": 2,
        "source_count": 4,
        "last_indexed": "2025-01-15T09:30:00Z"
      }
    ],
    "total": 42,
    "page": 1,
    "page_size": 25
  },
  "metadata": { ... }
}
```

**POST /api/v1/repos -- Register a repo**

```json
// Request
{
  "name": "webhook-api",
  "clone_url": "https://github.com/example/webhook-api.git",
  "domain_id": "domain_delivery",        // optional
  "default_branch": "main"               // optional, defaults to "main"
}

// Response (201 Created)
{
  "data": {
    "id": "repo_webhook-api",
    "name": "webhook-api",
    "clone_url": "https://github.com/example/webhook-api.git",
    "status": "registered",
    "freshness_state": "unknown"
  },
  "metadata": { ... }
}

// Response (409 Conflict) -- repo already registered with same clone_url
```

Side effect: triggers minimum T0+ bootstrap (README + manifest parsing, no LLM, ~30s).

**DELETE /api/v1/repos/{id} -- Deregister a repo**

```json
// Response (200 OK)
{
  "data": {
    "id": "repo_webhook-api",
    "status": "deregistered"
  },
  "metadata": { ... }
}
```

Side effect: soft delete — marks repo as deregistered, preserves knowledge for 30 days, then GC.

**GET /api/v1/repos/{id}/status -- Repo index status**

```json
// Response (200 OK)
{
  "data": {
    "id": "repo_webhook-api",
    "name": "webhook-api",
    "freshness_state": "fresh",
    "last_commit_indexed": "abc123def456",
    "component_count": 42,
    "flow_count": 7,
    "index_status": "complete",
    "estimated_extraction_cost_dollars": 0.14
  },
  "metadata": { ... }
}
```

**POST /api/v1/repos/{id}/extract -- Trigger manual extraction**

```json
// Response (202 Accepted)
{
  "data": {
    "job_id": "job_01HXYZ...",
    "estimated_duration_seconds": 120,
    "estimated_cost_dollars": 0.14
  },
  "metadata": { ... }
}
```

Triggers a cold-start or incremental extraction depending on current repo state.

#### 7.3.3 Domains

| Method | Path | Description | Async |
|---|---|---|---|
| `GET` | `/api/v1/domains` | List configured domains | No |
| `POST` | `/api/v1/domains` | Create a new domain | No |
| `GET` | `/api/v1/domains/:id` | Domain detail: repos, flows, stale areas | No |
| `PUT` | `/api/v1/domains/:id` | Update domain name or description | No |
| `DELETE` | `/api/v1/domains/:id` | Delete domain grouping (does not delete repos) | No |
| `POST` | `/api/v1/domains/:id/repos` | Add a repo to a domain | No |
| `DELETE` | `/api/v1/domains/:id/repos/:repo_id` | Remove a repo from a domain | No |

**POST /api/v1/domains -- Create a domain**

```json
// Request
{
  "name": "delivery",
  "description": "All delivery-related services",   // optional
  "repo_ids": ["repo_webhook-api", "repo_usage-metering"] // optional
}

// Response (201 Created)
{
  "data": {
    "id": "domain_delivery",
    "name": "delivery",
    "description": "All delivery-related services",
    "repo_count": 2
  },
  "metadata": { ... }
}
```

**PUT /api/v1/domains/{id} -- Update a domain**

```json
// Request
{
  "name": "delivery-platform",     // optional
  "description": "Updated description"  // optional
}

// Response (200 OK)
{
  "data": {
    "id": "domain_delivery",
    "name": "delivery-platform",
    "description": "Updated description"
  },
  "metadata": { ... }
}
```

**DELETE /api/v1/domains/{id} -- Delete a domain**

```
// Response (200 OK)
```

Note: does NOT delete repos; only removes the domain grouping.

**POST /api/v1/domains/{id}/repos -- Add a repo to a domain**

```json
// Request
{ "repo_id": "repo_webhook-api" }

// Response (200 OK)
{
  "data": {
    "domain_id": "domain_delivery",
    "repo_id": "repo_webhook-api",
    "status": "added"
  },
  "metadata": { ... }
}
```

**DELETE /api/v1/domains/{id}/repos/{repo_id} -- Remove a repo from a domain**

```json
// Response (200 OK)
{
  "data": { "status": "removed" },
  "metadata": { ... }
}
```

#### 7.3.4 Flows

| Method | Path | Description | Async |
|---|---|---|---|
| `GET` | `/api/v1/flows` | List known flows (paginated, filterable) | No |
| `GET` | `/api/v1/flows/:id` | Flow detail: segments, dependencies, observations | No |
| `POST` | `/api/v1/flows/trace` | Trace a flow by keyword (submits a task) | Yes |

#### 7.3.5 Impact and Dependencies

| Method | Path | Description | Async |
|---|---|---|---|
| `POST` | `/api/v1/impact` | Blast radius analysis (submits a task) | Yes |
| `POST` | `/api/v1/similar` | Find similar implementations (submits a task) | Yes |

**POST /api/v1/impact -- Blast radius**

```json
// Request
{
  "repo": "webhook-api",
  "path": "src/handlers/deliver.ts",
  "change_description": "Add idempotency key to deliver handler",
  "include_indirect": true,
  "max_depth": 3
}

// Response (202 Accepted) -- returns task reference
```

#### 7.3.6 Freshness and Refresh

| Method | Path | Description | Async |
|---|---|---|---|
| `GET` | `/api/v1/freshness/:scope` | Freshness/lag report for a scope | No |
| `POST` | `/api/v1/refresh` | Trigger knowledge refresh | Yes |

The `:scope` parameter accepts: `repo:<id>`, `domain:<id>`, `org`, `source:<type>:<id>`.

**POST /api/v1/refresh -- Trigger refresh**

```json
// Request
{
  "scope": "repo:webhook-api",
  "depth": "full",
  "include_sources": ["code", "tickets", "traces"]
}

// Response (202 Accepted) -- returns task reference
```

#### 7.3.7 Agents

| Method | Path | Description | Async |
|---|---|---|---|
| `GET` | `/api/v1/agents` | List active agents and health | No |
| `GET` | `/api/v1/agents/:id` | Agent detail: scope, sources, freshness, recent work | No |

#### 7.3.8 Search

| Method | Path | Description | Async |
|---|---|---|---|
| `GET` | `/api/v1/search` | Global search across repos, flows, concepts, docs | No |

```
GET /api/v1/search?q=idempotency+key&scope=domain:delivery&type=flow,concept
```

#### 7.3.9 Docs

| Method | Path | Description | Async |
|---|---|---|---|
| `GET` | `/api/v1/docs` | List docs with freshness/status | No |
| `GET` | `/api/v1/docs/:id` | Get a specific generated doc | No |
| `POST` | `/api/v1/docs/generate` | Generate/refresh docs (submits a task) | Yes |

#### 7.3.10 Admin

| Method | Path | Description | Async |
|---|---|---|---|
| `GET` | `/api/v1/admin/status` | Instance health, counts, version | No |
| `POST` | `/api/v1/admin/migrate` | Run pending database migrations | No |

#### 7.3.11 Feedback

| Method | Path | Description | Async |
|---|---|---|---|
| `POST` | `/api/v1/feedback` | Submit feedback on a completed task | No |
| `GET` | `/api/v1/feedback/:task_id` | Get all feedback submitted for a task | No |

**POST /api/v1/feedback -- Submit task feedback**

```json
// Request
{
  "task_id": "tsk_01HXYZ...",
  "quality_signal": "partially-helpful",
  "gap_reports": [{
    "scope": { "type": "repo", "id": "webhook-api" },
    "missing_knowledge": "Retry behavior after delivery-gateway timeout is undocumented",
    "gap_type": "undocumented_behavior",
    "severity": "blocking"
  }],
  "corrections": [{
    "target_entity_id": "entity:webhook-api:deliver-handler",
    "correction_type": "wrong_endpoint",
    "corrected_value": "src/handlers/deliver_v2.ts",
    "evidence": "The handler was moved in a recent refactor; old path no longer exists"
  }],
  "free_text": "The flow trace was mostly right but missed the async retry path"
}

// Response (201 Created)
{
  "data": {
    "feedback_id": "fb_01HXYZ...",
    "gap_reports_stored": 1,
    "corrections_stored": 1
  },
  "metadata": { ... }
}
```

Note: GapReport and Correction entities (S1) are created and stored as T1 entities. They inform the next extraction cycle for the relevant repos. Full learning loop is V2; V1 captures and stores.

**GET /api/v1/feedback/:task_id -- Get feedback for a task**

```json
// Response (200 OK)
{
  "data": {
    "feedback_id": "fb_01HXYZ...",
    "task_id": "tsk_01HXYZ...",
    "quality_signal": "partially-helpful",
    "gap_reports": [
      {
        "scope": { "type": "repo", "id": "webhook-api" },
        "missing_knowledge": "Retry behavior after delivery-gateway timeout is undocumented",
        "gap_type": "undocumented_behavior",
        "severity": "blocking"
      }
    ],
    "corrections": [
      {
        "target_entity_id": "entity:webhook-api:deliver-handler",
        "correction_type": "wrong_endpoint",
        "corrected_value": "src/handlers/deliver_v2.ts",
        "evidence": "The handler was moved in a recent refactor; old path no longer exists"
      }
    ]
  },
  "metadata": { ... }
}
```

**GET /api/v1/admin/status -- Instance status**

```json
// Response (200 OK) — Track A
{
  "data": {
    "org_id": "org_acme",
    "repo_count": 42,
    "domain_count": 7,
    "agent_runtime_track": "A",
    "version": "1.0.0",
    "status": "ok",
    "track": "A"
  },
  "metadata": { ... }
}

// Response (200 OK) — Track B (extended with session pool)
{
  "data": {
    "org_id": "org_acme",
    "repo_count": 42,
    "domain_count": 7,
    "agent_runtime_track": "B",
    "version": "1.0.0",
    "status": "ok",
    "track": "B",
    "session_pool": {
      "active": 2,
      "queued": 0,
      "max_concurrent": 3
    }
  },
  "metadata": { ... }
}
```

`session_pool` is present only when `track` is `"B"`. Monitoring tooling should treat its absence as equivalent to `{ active: 0, queued: 0, max_concurrent: 0 }` (i.e., Track A has no pool to report).

**POST /api/v1/admin/migrate -- Run database migrations**

```json
// Response (200 OK)
{
  "data": {
    "migrations_applied": ["0003_add_freshness_index", "0004_add_extraction_jobs"]
  },
  "metadata": { ... }
}
```

### 7.4 Pagination

All list endpoints support cursor-based pagination:

```
GET /api/v1/repos?page_size=25&cursor=eyJpZCI6NDJ9
```

Response includes:

```json
{
  "data": {
    "items": [ ... ],
    "total": 142,
    "page_size": 25,
    "next_cursor": "eyJpZCI6Njd9",
    "has_more": true
  }
}
```

### 7.5 Filtering and Sorting

List endpoints accept query parameters for filtering and sorting:

```
GET /api/v1/repos?domain=delivery&freshness=stale&sort=last_indexed&order=desc
```

Common filter parameters: `domain`, `freshness`, `status`, `source_type`, `created_after`, `created_before`.
Common sort fields: `name`, `last_indexed`, `created_at`, `freshness`.

### 7.6 Rate Limiting

Rate limits are enforced per API key or user token:

| Tier | Requests/minute | Concurrent tasks |
|---|---|---|
| Default | 120 | 5 |
| Premium | 600 | 20 |

Rate limit headers are included in every response:

```
X-RateLimit-Limit: 120
X-RateLimit-Remaining: 118
X-RateLimit-Reset: 1712300400
```

When exceeded, the API returns `429 Too Many Requests` with a `Retry-After` header.

### 7.7 Error Response Format

Errors use the same envelope structure with a null `data` field and an `error` object:

```json
{
  "data": null,
  "error": {
    "code": "TASK_NOT_FOUND",
    "message": "No task found with ID tsk_01HXYZ...",
    "details": {},
    "request_id": "req_abc123"
  },
  "metadata": {
    "confidence": "confirmed",
    "freshness": { "overall": "unknown", "details": [] },
    "evidence_summary": [],
    "gaps": [],
    "conflicts": [],
    "cache_status": "no_cache",
    "task_id": "",
    "duration_ms": 3
  }
}
```

Standard error codes:

| HTTP Status | Code | Description |
|---|---|---|
| 400 | `INVALID_REQUEST` | Malformed request body or parameters |
| 401 | `UNAUTHORIZED` | Missing or invalid auth token |
| 403 | `FORBIDDEN` | Valid token but insufficient permissions |
| 404 | `NOT_FOUND` | Resource does not exist |
| 409 | `CONFLICT` | Operation conflicts with current state |
| 422 | `INVALID_OPERATION` | Unknown operation or invalid params for operation |
| 429 | `RATE_LIMITED` | Rate limit exceeded |
| 500 | `INTERNAL_ERROR` | Unexpected server error |
| 503 | `SERVICE_UNAVAILABLE` | System is overloaded or in maintenance |

### 7.8 API Versioning

All API endpoints are versioned under `/api/v{N}/`.

**Breaking change definition:** A breaking change is any of:
- Removing or renaming a field from a response
- Changing a field's type
- Removing an endpoint
- Changing required → optional is NOT breaking
- Adding new optional fields is NOT breaking

**Deprecation policy:**
- Breaking changes require a new version (`/api/v2/`)
- Deprecated v1 endpoints remain functional for minimum 6 months after v2 launch
- Deprecation is signaled via `Bifrost-Deprecation: {date}` response header
- Deprecated endpoints return `"deprecated": true` in response metadata

**V1 availability target:** 99.5% uptime (single-instance deployment). Multi-instance for HA is V2.

---

## 8. CLI Specification

### 8.1 Overview

The CLI is a Python package (`bifrost`) built with Typer that wraps the REST API. It is installed via `uv pip install bifrost` and provides a `bifrost` entry point. It adds convenience features for developer workflows: human-readable output formatting, local workspace integration, and an interactive shell mode.

```
bifrost <command> [subcommand] [options]
```

Typer app definition:

```python
import typer

app = typer.Typer(name="bifrost", help="Bifrost engineering knowledge platform CLI.")
```

### 8.2 Global Options

Global options are defined as a Typer callback:

```python
@app.callback()
def main(
    output: str = typer.Option("human", "-o", "--output", help="Output format: human (default), json, yaml"),
    server: str = typer.Option(None, "-s", "--server", help="API server URL (default from config)"),
    token: str = typer.Option(None, "-t", "--token", help="Auth token (default from config or keychain)"),
    include_local: bool = typer.Option(False, "--include-local", help="Include uncommitted local workspace changes"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Verbose output with evidence details"),
    no_stream: bool = typer.Option(False, "--no-stream", help="Disable SSE streaming; poll instead"),
    timeout: int = typer.Option(120, "--timeout", help="Max wait time for async tasks in seconds"),
) -> None:
    ...
```

| Option | Short | Description |
|---|---|---|
| `--output` | `-o` | Output format: `human` (default), `json`, `yaml` |
| `--server` | `-s` | API server URL (default from config) |
| `--token` | `-t` | Auth token (default from config or keychain) |
| `--include-local` | | Include uncommitted local workspace changes in analysis |
| `--verbose` | `-v` | Verbose output with evidence details |
| `--no-stream` | | Disable SSE streaming for async tasks; poll instead |
| `--timeout` | | Max wait time for async tasks (default: 120s) |

### 8.3 Command Tree

#### 8.3.0 Init -- First-Run Initialization

```
bifrost init [options]
```

Typer command definition:

```python
@app.command()
def init(
    org_name: str = typer.Option(None, "--org-name", help="Organization name (skips interactive prompt)"),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Fail instead of prompting"),
    skip_llm_check: bool = typer.Option(False, "--skip-llm-check", help="Skip Anthropic API key validation"),
) -> None:
    """Initialize a new Bifrost instance. Run once on first deployment."""
    ...
```

Initializes a new Bifrost instance. Run once on first deployment.

**Steps performed:**

1. Reads environment variables (`DATABASE_URL`, `ANTHROPIC_API_KEY`, `BIFROST_TRACK`)
2. Runs database migrations via Alembic (creates all tables from S4 schema)
3. Creates the Organization entity (prompts for org name if interactive)
4. Validates Anthropic API key (test LLM call)
5. Validates database connectivity
6. Outputs: instance ID, admin token, suggested next steps

**Options:**

| Option | Description |
|---|---|
| `--org-name NAME` | Organization name (skips interactive prompt) |
| `--non-interactive` | Fail instead of prompting for missing values |
| `--skip-llm-check` | Skip Anthropic API key validation |

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Database connection failed |
| 2 | Anthropic API key invalid |
| 3 | Database migration failed |

#### 8.3.1 Ask -- Natural Language Questions

```python
@app.command()
def ask(
    question: str = typer.Argument(..., help="Natural language question"),
    scope: str = typer.Option(None, "--scope", help="Scope filter (e.g. domain:delivery)"),
    include_local: bool = typer.Option(False, "--include-local", help="Include local workspace changes"),
) -> None:
    """Ask a natural language question about your codebase."""
    ...
```

```
bifrost ask "how does webhook delivery work?"
bifrost ask "what depends on the User model?" --scope domain:delivery
bifrost ask "what changed in webhook-api this week?" --include-local
```

Submits a task with the appropriate operation inferred from the question. Streams progress via the SSE endpoint (`GET /api/v1/tasks/{task_id}/stream`), then prints the result. SSE events are rendered as:

- `agent.consulting` -- show spinner: `Consulting {repo}...`
- `text.delta` -- stream text content directly to stdout as it arrives
- `task.complete` -- show confidence level and freshness summary footer
- `task.error` -- print error message and exit with code 4

Example output while streaming:

```
Consulting dispatcher...
Consulting signing-keys...
Consulting usage-metering...

The delivery flow starts at web-frontend/src/webhooks/trigger.ts and calls...

[Confidence: HIGH | Data source: fresh | Duration: 14.2s]
```

#### 8.3.2 Trace -- Flow Tracing

```
bifrost trace "webhook delivery"
bifrost trace "user signup" --scope domain:identity --max-depth 5
bifrost trace "webhook retry" --include-runtime
```

#### 8.3.3 Impact -- Blast Radius Analysis

```
bifrost impact --repo webhook-api --path src/handlers/deliver.ts
bifrost impact --repo webhook-api --path src/handlers/deliver.ts --change "add idempotency key"
bifrost impact --repo webhook-api --include-indirect --max-depth 3
```

#### 8.3.4 Deps -- Dependency Analysis

```
bifrost deps --repo webhook-api
bifrost deps --repo webhook-api --direction both --type api,schema
bifrost deps --domain delivery --include-indirect
```

#### 8.3.5 Similar -- Find Similar Code

```
bifrost similar --repo webhook-api --path src/handlers/deliver.ts
bifrost similar --concept "retry with exponential backoff" --scope domain:delivery
```

#### 8.3.6 Plan -- Change Planning

```
bifrost plan "add idempotency to webhook delivery" --repo webhook-api
bifrost plan "migrate user auth to OAuth2" --domain identity
```

#### 8.3.7 Status -- Freshness and Lag

```
bifrost status
bifrost status --repo webhook-api
bifrost status --domain delivery
bifrost status --repo webhook-api --source code
```

#### 8.3.8 Refresh -- Trigger Knowledge Refresh

```
bifrost refresh --repo webhook-api
bifrost refresh --domain delivery
bifrost refresh --repo webhook-api --source code,tickets
bifrost refresh --repo webhook-api --depth full
```

#### 8.3.9 Docs -- Documentation Management

```
bifrost docs generate --repo webhook-api
bifrost docs generate --repo webhook-api --type onboarding,flow
bifrost docs status --repo webhook-api
bifrost docs status --domain delivery
```

#### 8.3.10 Repos -- Repository Management

```python
repos_app = typer.Typer(help="Repository management.")
app.add_typer(repos_app, name="repos")

@repos_app.command("list")
def repos_list(
    domain: str = typer.Option(None, "--domain", help="Filter by domain"),
    freshness: str = typer.Option(None, "--freshness", help="Filter by freshness state"),
) -> None:
    """List registered repositories."""
    ...

@repos_app.command("show")
def repos_show(
    repo_id: str = typer.Argument(..., help="Repository ID or name"),
    include: str = typer.Option(None, "--include", help="Comma-separated: flows,deps,patterns"),
) -> None:
    """Show detailed repository information."""
    ...
```

```
bifrost repos list
bifrost repos list --domain delivery --freshness stale
bifrost repos show webhook-api
bifrost repos show webhook-api --include flows,deps,patterns
```

#### 8.3.11 Domains -- Domain Management

```python
domains_app = typer.Typer(help="Domain management.")
app.add_typer(domains_app, name="domains")

@domains_app.command("list")
def domains_list() -> None:
    """List configured domains."""
    ...

@domains_app.command("show")
def domains_show(domain_id: str = typer.Argument(..., help="Domain ID or name")) -> None:
    """Show detailed domain information."""
    ...
```

```
bifrost domains list
bifrost domains show delivery
```

#### 8.3.12 Agents -- Agent Visibility

```python
agents_app = typer.Typer(help="Agent visibility and management.")
app.add_typer(agents_app, name="agents")

@agents_app.command("list")
def agents_list(
    scope: str = typer.Option(None, "--scope", help="Filter by scope (e.g. repo:webhook-api)"),
) -> None:
    """List active agents and their health."""
    ...

@agents_app.command("show")
def agents_show(agent_id: str = typer.Argument(..., help="Agent ID")) -> None:
    """Show detailed agent information."""
    ...
```

```
bifrost agents list
bifrost agents list --scope repo:webhook-api
bifrost agents show agent:webhook-api
```

#### 8.3.13 Tasks -- Task Management

```python
tasks_app = typer.Typer(help="Task management.")
app.add_typer(tasks_app, name="tasks")

@tasks_app.command("list")
def tasks_list(
    status_filter: str = typer.Option(None, "--status", help="Filter by status"),
) -> None:
    """List recent tasks."""
    ...

@tasks_app.command("show")
def tasks_show(task_id: str = typer.Argument(..., help="Task ID")) -> None:
    """Show task status and result."""
    ...

@tasks_app.command("cancel")
def tasks_cancel(task_id: str = typer.Argument(..., help="Task ID")) -> None:
    """Cancel a running task."""
    ...
```

```
bifrost tasks list
bifrost tasks list --status running
bifrost tasks show tsk_01HXYZ...
bifrost tasks cancel tsk_01HXYZ...
```

#### 8.3.14 Auth -- Authentication

```python
auth_app = typer.Typer(help="Authentication management.")
app.add_typer(auth_app, name="auth")

@auth_app.command()
def login(
    server: str = typer.Option(None, "--server", help="API server URL"),
) -> None:
    """Authenticate with a Bifrost server."""
    ...

@auth_app.command()
def status() -> None:
    """Show current authentication status."""
    ...

@auth_app.command()
def logout() -> None:
    """Clear stored credentials."""
    ...
```

```
bifrost auth login
bifrost auth login --server https://bifrost.example.com
bifrost auth status
bifrost auth logout
```

Stores credentials via the `keyring` library (OS keychain) or falls back to `~/.bifrost/credentials`.

#### 8.3.15 Shell -- Interactive Mode

```
bifrost shell
```

Opens a conversational REPL where the user can ask follow-up questions, refine scope, and issue commands without re-typing context. The shell maintains session state including the most recent task, repo context, and domain scope.

Example session:

```
bifrost> trace "webhook delivery"
[streaming results...]

bifrost> what if we add a retry here?
[impact analysis...]

bifrost> show deps for that handler
[dependency output...]

bifrost> exit
```

#### 8.3.16 Feedback -- Submit Task Feedback

```python
@app.command()
def feedback(
    task_id: str = typer.Argument(None, help="Task ID (defaults to most recent task)"),
    helpful: bool = typer.Option(False, "--helpful", help="Mark as helpful"),
    partial: bool = typer.Option(False, "--partial", help="Mark as partially helpful"),
    unhelpful: bool = typer.Option(False, "--unhelpful", help="Mark as unhelpful"),
    gap: list[str] = typer.Option([], "--gap", help="Report a knowledge gap (repeatable)"),
    correction: str = typer.Option(None, "--correction", help="Correction: 'what was wrong → what is right'"),
) -> None:
    """Submit feedback on a completed task result."""
    ...
```

Submit feedback on the most recent (or specified) task result. If `task-id` is omitted, applies to the most recent task in the current session.

Example:

```
bifrost feedback tsk_01HXYZ... --partial \
  --gap "retry behavior after delivery-gateway timeout is not documented" \
  --correction "deliver handler is at src/handlers/deliver_v2.ts, not deliver.ts"
```

Calls `POST /api/v1/feedback`. Prints the `feedback_id` on success.

### 8.4 Output Formatting

**Human mode** (default): Structured text with headers, indentation, color coding for confidence levels, and freshness indicators.

```
Webhook Delivery Flow
=====================
Confidence: HIGH | Freshness: FRESH | Duration: 14.2s

Entry: web-frontend/src/webhooks/trigger.ts:28
  -> webhook-api/src/handlers/deliver.ts:42  [sync, HTTP]
  -> delivery-gateway/src/send.ts:15          [sync, HTTP]
  -> notifications-svc/src/email/receipt.ts:8  [async, Kafka]

Evidence: 4 sources (2 code, 1 trace, 1 config)
Gaps: 1 significant -- retry behavior after delivery-gateway timeout unknown
Conflicts: none

[Use --verbose for full evidence details]
```

**JSON mode** (`-o json`): Raw API response envelope, suitable for piping to `jq` or consumption by scripts and agents.

**YAML mode** (`-o yaml`): Same data as JSON, in YAML format for readability.

### 8.5 Local Workspace Integration

When `--include-local` is passed, the CLI:

1. Detects the current git repo and workspace state (uncommitted changes, local branch vs remote default).
2. Sends workspace diff data alongside the task request.
3. The response clearly annotates which findings incorporate local-only state.
4. Local state is never treated as authoritative -- it is flagged as divergent from remote truth.

### 8.6 Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | General error |
| 2 | Invalid arguments or usage |
| 3 | Authentication failure |
| 4 | Task failed or timed out |
| 5 | Server unreachable |

---

## 9. Control Plane UI Specification

### 9.1 Overview

| Property | Value |
|---|---|
| Technology | React + TypeScript (frontend), FastAPI (backend API) |
| State management | Server state via API (React Query / SWR); local UI state only for ephemeral concerns |
| Real-time | SSE for task progress and freshness updates (FastAPI `StreamingResponse`) |
| Auth | The identity providers in 7.2. The `bifrost_session` cookie is only a rate-limit and audit key |

The UI is a read-heavy dashboard with task submission and refresh trigger capabilities. It does not implement any business logic -- all operations are API calls.

### 9.2 Pages

#### 9.2.1 Dashboard

The landing page provides organization-level health at a glance.

**Sections:**
- **Health summary**: total repos, domains, agents; counts by freshness state (fresh / slightly stale / stale / very stale).
- **Freshness heatmap**: grid of repos (rows) by source type (columns), color-coded by freshness state. Clicking a cell navigates to the repo view filtered by that source.
- **Recent tasks**: last 10 tasks with status, operation, duration, confidence. Clicking navigates to task view.
- **Stale areas**: list of repos or domains with the worst lag, sorted by severity. Links to respective detail views.
- **Quick actions**: task submission form (natural language question input), refresh all button.

#### 9.2.2 Repo View

Detailed view for a single repository.

**Sections:**
- **Header**: repo name, domain, overall freshness badge, last indexed timestamp.
- **Sources**: table of connected sources (type, identifier, freshness, last sync). Each row has a "refresh" action.
- **Agents**: list of specialists covering this repo with health status.
- **Freshness/lag detail**: breakdown by source type showing lag_seconds and state.
- **Docs**: table of generated docs (type, status, last refreshed, staleness). "Generate" and "Refresh" buttons per doc.
- **Learned patterns**: list of repo-specific patterns and conventions discovered by agents.
- **Flow coverage**: flows that touch this repo, with participation summary.
- **Dependencies**: direct dependencies (outbound and inbound) with type badges.
- **Recent changes**: recent commits/PRs and their effect on knowledge state (what was re-indexed, what became stale).

#### 9.2.3 Domain View

Detailed view for a domain.

**Sections:**
- **Header**: domain name, repo count, overall freshness badge.
- **Repos**: list of repos in this domain with freshness badges. Clicking navigates to repo view.
- **Cross-repo flows**: flows that span multiple repos within this domain, with visual segment indicators.
- **Cross-repo dependencies**: dependency edges that cross repo boundaries.
- **Stale areas**: repos or sources within the domain that are most stale.
- **Domain docs**: domain-level generated documentation with staleness indicators.
- **Common patterns**: patterns shared across repos in this domain.
- **Migrations**: active migration state if any (status, progress, affected repos).

#### 9.2.4 Task View

Detailed view for a single task.

**Sections:**
- **Header**: task ID, operation, status badge, submitted timestamp, duration.
- **Question/request**: the original input.
- **Progress** (if running): SSE-driven progress bar with phase labels and agent activity.
- **Agents consulted**: list of agents that participated, with per-agent contribution summary.
- **Evidence**: expandable list of evidence references grouped by source type, with snippets.
- **Confidence and freshness**: visual display of confidence level and per-source freshness.
- **Conflicts**: if any, displayed with both sides and their evidence.
- **Gaps**: listed with severity and suggestions.
- **Result**: the synthesized output, formatted based on operation type (flow diagram for traces, tree for dependencies, prose for understanding, etc.).

#### 9.2.5 Flow Explorer

Visual exploration of flows.

**Sections:**
- **Flow DAG**: directed acyclic graph visualization of flow segments. Nodes represent repos/services, edges represent calls (sync/async). Color-coded by freshness.
- **Segment detail panel**: clicking a node or edge shows: source file, line, handler, dependencies, runtime observations if available.
- **Comparison mode**: side-by-side display of two flow versions (old vs new, static vs observed, repo A vs repo B).
- **Filters**: filter by domain, repo, flow type, freshness state.

*Note: Full visualization deferred to post-V1. V1 provides a simplified list/tree view of flow segments.*

#### 9.2.6 Dependency Explorer

Interactive dependency graph visualization.

**Sections:**
- **Dependency graph**: interactive force-directed or hierarchical graph. Nodes are repos or modules, edges are typed dependencies (code, API, schema, event, etc.). Color-coded by dependency type.
- **Blast radius overlay**: select a node to highlight all direct and indirect dependents, with confidence annotations.
- **Filters**: filter by dependency type, direction, depth, confidence threshold.
- **Detail panel**: clicking an edge shows evidence for the dependency, freshness, and confidence.

*Note: Full interactive graph deferred to post-V1. V1 provides a table-based dependency view.*

#### 9.2.7 Docs Manager

Documentation lifecycle management.

**Sections:**
- **Doc inventory**: table of all generated docs with columns: scope, type, status (draft/published/stale), last refreshed, freshness vs source state.
- **Staleness alerts**: docs that are stale relative to their source, sorted by severity.
- **Generation controls**: buttons to generate or refresh docs for a selected scope and type.
- **Preview**: rendered preview of a generated doc before publication.

#### 9.2.8 Admin

System configuration and administration.

**Sections:**
- **Source configuration**: table of registered sources (type, URL, credentials status, sync schedule, last sync). Add/edit/remove sources.
- **Agent configuration**: list of agents with scope assignments, health, and configuration. Start/stop agents.
- **Refresh controls**: manual refresh triggers at org, domain, repo, or source level. Schedule configuration.
- **Audit log**: searchable log of administrative actions, refresh events, and configuration changes.

### 9.3 Real-Time Updates

The UI uses Server-Sent Events (SSE) for:

- **Task progress**: when viewing a running task, the Task View subscribes to `/api/v1/tasks/:id/stream` for live progress updates.
- **Freshness updates**: the Dashboard and Repo View subscribe to a freshness event stream that pushes state changes when source re-indexing completes.
- **Agent health**: the Admin and Agent list pages receive live health pings.

### 9.4 Global Search

A persistent search bar in the top navigation supports:

```
Search: "idempotency key" [All | Repos | Flows | Concepts | Docs]
```

Search is backed by `GET /api/v1/search` and returns categorized results with relevance scores and freshness indicators.

---

## 10. Interface Contracts

### 10.1 OpenAPI Specification

A machine-readable OpenAPI 3.1 specification describes all REST API endpoints, request/response schemas, and error codes. It is auto-generated by FastAPI from Pydantic models and route decorators — no manual maintenance or plugin needed. Access it at runtime via `GET /docs` (Swagger UI) or `GET /openapi.json`.

### 10.2 CLI Man Pages

Each CLI command has a corresponding man page generated from the command definition. Available via:

```
bifrost help <command>
bifrost <command> --help
```

### 10.3 UI Component API

UI components consume the API via a typed TypeScript client library generated from the OpenAPI spec (e.g., using `openapi-typescript-codegen`). This guarantees type safety between the FastAPI backend and React frontend layers.

---

## 11. Dependencies on Other Specifications

| Dependency | Spec | What This Spec Needs |
|---|---|---|
| Orchestration | S9 (Orchestration) | Task lifecycle, agent consultation protocol, synthesis |
| Knowledge Model | S1 (Knowledge Model) | Entity types, relationship types, confidence model |
| Flow Model | S5 (Flow Model) | Flow structure, segment types, comparison semantics |
| Dependency Model | S6 (Dependency Model) | Dependency types, graph traversal, blast radius calculation |
| Storage | S4 (Storage) | Cache status reporting, freshness state computation |
| Source Adapters | S2 (Source Adapters) | Source types, freshness tracking per source |
| Agent Model | S8 (Agent Workforce) | Agent types, health reporting, scope definitions |

---

## 12. Key Decisions

| Decision | Rationale |
|---|---|
| **REST-first, GraphQL deferred** | REST covers all V1 use cases. GraphQL adds value for complex graph traversals (multi-hop dependency queries, flow exploration) but introduces schema complexity. Defer until the knowledge graph query patterns stabilize. |
| **SSE for streaming, not WebSocket** | Task progress is unidirectional (server to client). SSE is simpler to implement, proxy, and debug. WebSocket would be needed only for a bidirectional interactive agent chat, which is not in V1 scope. |
| **CLI uses the same API as UI** | No special backend paths for any surface. This guarantees feature parity, simplifies testing, and makes the API a reliable integration point for third-party agents. |
| **Response envelope is identical across all surfaces** | The CLI and UI may render the envelope differently, but the data contract is the same. This means an AI agent consuming the JSON output of the CLI gets the same structure as one calling the API directly. |
| **Async-first for agent-involving operations** | Any operation that triggers specialist consultation is inherently variable-duration. Modeling these as async tasks with SSE streaming avoids HTTP timeout issues and provides observability into agent activity. |
| **Cursor-based pagination** | Offset-based pagination breaks under concurrent writes. Cursor-based pagination is stable and efficient for the append-heavy workloads typical of task and event listing. |

---

## 13. V1 Scope

### Included in V1

- **REST API**: all endpoints listed in section 7, fully functional.
- **CLI**: all commands listed in section 8, fully functional. Human and JSON output modes. SSE streaming for all async task commands (`ask`, `trace`, `impact`, `plan`, etc.).
- **Control Plane UI**: basic dashboard with:
  - Repo list with freshness indicators.
  - Domain list.
  - Task submission (natural language input).
  - Task list and task detail view.
  - Freshness status overview.
  - Basic refresh triggers.
- **Streaming (Track A)**: V1 API and CLI implement Track A streaming (AsyncGenerator → SSE). All six SSE event types (`task.started`, `agent.consulting`, `agent.found`, `text.delta`, `task.complete`, `task.error`) are defined and implemented. Track B streaming (Managed Agent session proxy) is an internal implementation change with no client-facing API changes; it is deferred to V1.5 when Managed Agents is enabled.

### Deferred to Post-V1

| Feature | Reason |
|---|---|
| GraphQL API | Query patterns not yet stabilized |
| Flow Explorer visualization (DAG) | Requires graph rendering library integration; V1 uses list/tree view |
| Dependency Explorer (interactive graph) | Same as above; V1 uses table-based view |
| Docs Manager preview and publication workflow | V1 generates docs; full lifecycle management deferred |
| CLI `shell` interactive mode | Requires session state management; V1 supports single-command invocations |
| CLI YAML output mode | Low priority; JSON covers programmatic needs |
| Admin page (source/agent config) | V1 configuration is file/env-based; UI admin deferred |

---

## 14. Future Evolution

| Surface | Planned Enhancement |
|---|---|
| **API** | GraphQL endpoint for flexible knowledge graph queries. Batch task submission. Webhook callbacks for task completion. |
| **API** | MCP (Model Context Protocol) server integration so Bifrost can act as a tool provider for LLM agents. |
| **CLI** | Interactive shell mode with session context. Plugin system for custom output formatters. |
| **CLI** | IDE extensions (VS Code, JetBrains) that wrap CLI commands with in-editor UI. |
| **UI** | Flow Explorer with full DAG visualization, animated runtime replay. |
| **UI** | Dependency Explorer with interactive graph, what-if blast radius simulation. |
| **UI** | Slack bot integration for task submission and result delivery. |
| **UI** | Embeddable widgets for internal dashboards. |

---

## 15. Open Questions

| # | Question | Impact | Notes |
|---|---|---|---|
| 1 | Should the API support long-polling as an alternative to SSE for environments where SSE is blocked? | API design | Could add `?poll=true` mode to task endpoints |
| 2 | What is the auth story for CLI in CI/CD pipelines? | CLI auth | Service account tokens vs. machine identity |
| 3 | Should the UI support multiple concurrent task streams, or one at a time? | UI complexity | Multiple streams increase frontend complexity |
| 4 | How should `--include-local` work when the CLI is invoked outside a git repo? | CLI edge case | Probably error with a clear message |
| 5 | Should the GraphQL schema be auto-generated from the knowledge model, or hand-authored? | Future API | Auto-generation reduces drift but may produce a noisy schema |
| 6 | What granularity of RBAC is needed for V1? Repo-level? Domain-level? Operation-level? | Auth model | Affects API middleware design |
| 7 | Should SSE streams include intermediate agent reasoning, or only phase/progress summaries? | Privacy, UX | Full reasoning is useful for debugging but may leak internal details |
| 8 | What is the maximum task duration before automatic timeout? | Orchestration | Affects UX expectations and resource planning |
