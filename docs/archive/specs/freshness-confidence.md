# S3 — Freshness, Confidence, and Lag Model

**Spec ID:** S3

## 1. Purpose and Scope

This specification defines how Bifrost tracks, computes, and exposes the currency and trustworthiness of every piece of knowledge it holds. It is the "honesty layer" — the mechanism that prevents Bifrost from ever silently presenting stale, uncertain, or conflicting information as settled truth.

Every answer Bifrost produces must declare:
- **How current** the underlying knowledge is (freshness and lag).
- **How trustworthy** the answer is (confidence).
- **Whether disagreements exist** among contributing sources (conflicts).

Without this layer, Bifrost is just another system that guesses and hopes. With it, every consumer — human or agent — can make informed decisions about whether to trust, verify, or refresh before acting.

### Scope boundaries

This spec covers:
- Freshness tracking for sources, extracted knowledge, and generated docs.
- Lag computation and severity classification.
- Confidence scoring and categorical levels.
- Conflict detection taxonomy and representation.
- Invalidation propagation model.
- Update trigger taxonomy.
- Local vs remote divergence handling.

This spec does NOT cover:
- The knowledge model itself (see S1 — Knowledge Model).
- Source adapter implementation details (see S2 — Source Adapters).
- Orchestration decisions about when to refresh (see S5 — Orchestration).
- UI presentation of freshness/confidence (see S9 — Control Plane UI).

---

## 2. Glossary References

| Term | Definition |
|------|-----------|
| **T1 (Tier 1)** | Raw extracted facts — entities, relationships, dependency edges, flow hints — derived directly from source content. |
| **T2 (Tier 2)** | Derived knowledge — cross-referenced, composed, or inferred understanding built from T1 facts. |
| **T3 (Tier 3)** | Generated artifacts — human-readable docs, summaries, playbooks — produced from T2 knowledge. |
| **Source revision** | An opaque identifier representing a point-in-time state of a source object (commit SHA, modification timestamp, ETag, version number). |
| **Indexed revision** | The source revision that Bifrost last successfully processed and extracted knowledge from. |
| **Freshness** | The relationship between the current source state and the state Bifrost has indexed. |
| **Lag** | The measurable gap between current source state and Bifrost's indexed state. |
| **Confidence** | A declaration of how trustworthy a piece of knowledge is, given its source basis, freshness, and agreement with other sources. |
| **Staleness** | The condition where source state has advanced beyond what Bifrost has indexed. |
| **TTL** | Time-to-live — a configurable duration after which knowledge degrades from confirmed to stale absent re-validation. |

---

## 3. Requirements Traceability

### From S12 — Freshness and Update Requirements

| Requirement | Section in this spec |
|------------|---------------------|
| 12.1 Freshness must be explicit | S5 Freshness Model, S7 Confidence Model |
| 12.2 Source state tracking | S5 Freshness Model |
| 12.3 Lag requirements | S6 Lag Computation |
| 12.4 Update trigger requirements | S10 Update Triggers |
| 12.5 Invalidation requirements | S9 Invalidation Model |
| 12.6 Update granularity requirements | S9 Selective Invalidation |
| 12.7 Local vs remote requirements | S11 Local vs Remote |

### From S16 — Confidence, Uncertainty, and Conflict Requirements

| Requirement | Section in this spec |
|------------|---------------------|
| 16.1 Confidence must be explicit | S7 Confidence Model |
| 16.2 Conflict classes | S8 Conflict Detection |
| 16.3 Plan confidence requirements | S7 Confidence Model (composition), S8 Conflict Detection |

### Cross-cutting traceability

| Requirement area | Section in this spec |
|-----------------|---------------------|
| 7.2 Knowledge quality (source basis, freshness, confidence, gaps) | S5, S6, S7 |
| 8.4 Docs freshness requirements | S5 Doc freshness |
| 13.4 Cache invalidation requirements | S9 Invalidation Model |
| 13.5 Storage visibility (what came from cache, stale cache) | S7 Confidence Model |
| 17.3 Runtime freshness | S5 Freshness Model, S6 Lag Computation |
| 22.1 Correctness (grounded, current-enough, evidence-backed) | Entire spec |

---

## 4. Design Goals and Non-Goals

### Goals

1. **Never silently stale.** Every knowledge artifact carries explicit freshness metadata. No consumer should ever receive information without knowing how current it is.

2. **Cheap to check, expensive only when needed.** Freshness checks should be lightweight (compare revision identifiers). Full re-extraction should happen only when staleness is confirmed.

3. **Transitive honesty.** If a T1 fact is stale, all T2 knowledge derived from it is transitively stale, and all T3 docs built from that T2 are transitively stale. The system must propagate, not hide.

4. **Configurable thresholds.** Different source types have different acceptable staleness windows. Code repos may tolerate minutes; design docs may tolerate days.

5. **Composable confidence.** When multiple sources contribute to a single answer, the system must compose their individual confidences into a meaningful aggregate.

6. **Conflict surfacing over conflict hiding.** When sources disagree, the system must surface the disagreement rather than silently picking a winner.

### Non-Goals

1. **Perfect real-time synchronization.** Bifrost is not a CDC (change data capture) pipeline. It is eventually consistent with bounded staleness.

2. **Automatic conflict resolution.** V1 detects and surfaces conflicts. It does not auto-resolve them.

3. **Bayesian or probabilistic confidence modeling.** V1 uses simple deterministic rules. Probabilistic models are future work.

4. **Sub-second freshness guarantees.** The system optimizes for "current enough for the task" not "real-time mirroring."

---

## 5. Freshness Model

Freshness tracks the temporal relationship between three tiers of knowledge and their upstream sources.

### 5.1 Source Freshness

Source freshness measures the gap between the latest known source revision and the latest revision Bifrost has indexed.

**Representation:**

```python
class SourceFreshness(BaseModel):
    source_id: str
    source_type: SourceType
    scope: SourceScope  # has optional repo_id, domain_id

    # What Bifrost has indexed
    indexed_at: datetime
    indexed_revision: str          # commit SHA, ETag, version, etc.

    # What the source currently reports
    source_checked_at: datetime
    source_current_revision: str

    # Derived
    is_fresh: bool                 # indexed_revision == source_current_revision
    lag_seconds: float | None      # None when revision-based comparison suffices
```

A source is **fresh** when `indexed_revision == source_current_revision`. It is **stale** when they differ.

### 5.2 Knowledge Freshness (T2)

Knowledge freshness tracks when derived knowledge was last recomputed from its contributing T1 facts.

```python
class KnowledgeFreshness(BaseModel):
    knowledge_id: str
    knowledge_type: KnowledgeType
    computed_at: datetime
    source_facts: list[SourceFactReference]   # T1 facts this was derived from
    all_sources_fresh: bool                   # True only if every contributing T1 fact is fresh
    stalest_source_lag: LagSeverity           # worst lag among contributing sources
```

T2 knowledge is only as fresh as its stalest contributing T1 fact.

### 5.3 Doc Freshness (T3)

Doc freshness tracks when generated documents were last refreshed from the T2 knowledge they present.

```python
class DocFreshness(BaseModel):
    doc_id: str
    generated_at: datetime
    source_knowledge: list[KnowledgeReference]  # T2 items this doc presents
    all_knowledge_fresh: bool
    stalest_knowledge_lag: LagSeverity
    doc_state: Literal['current', 'stale', 'critically_stale', 'never_generated']
```

### 5.4 Per-Scope Rollups

Freshness rolls up hierarchically. Each level takes the **worst** freshness across its children.

| Scope | Rollup rule |
|-------|-------------|
| **Source object** | Individual freshness as defined in 5.1. |
| **Repo** | Worst freshness across all source objects scoped to that repo. |
| **Domain** | Worst freshness across all repos in the domain. |
| **Org** | Worst freshness across all domains. |

This means: if one source in one repo in a domain is critically stale, the domain-level freshness reflects that. Consumers can drill down to find the specific stale source.

### 5.5 Freshness State Machine

A source object moves through the following states:

```
                    +-----------+
         +--------->|  Unknown  |<---------+
         |          +-----------+          |
         |               |                |
         |          first index           |
         |               |                |
         |               v                |
   source deleted   +-----------+   index failure
   or unreachable   |   Fresh   |   (after retries)
         |          +-----------+          |
         |            |       ^            |
         |     source |       | re-index   |
         |     changed|       | succeeds   |
         |            v       |            |
         |          +-----------+          |
         +--------- |   Stale   |----------+
                    +-----------+
                         |
                    lag exceeds
                    critical threshold
                         |
                         v
                  +----------------+
                  | Critical Stale |
                  +----------------+
```

### 5.6 Freshness State Document (AgentStateBackend)

Stored at: `/repo/{repo-id}/freshness/state.md`

(Track A: PostgreSQL `knowledge_text_cache` at this path key. Track B: Anthropic Memory Store at `/repo/{id}/freshness`.)

Content format (structured text, not JSON, for readability by LLMs):

```
last_commit_indexed: abc123def456789
last_indexed_at: 2025-01-15T10:00:00Z
index_status: complete
component_cursor: null
component_count: 47
flow_count: 12
pending_commits: 3
freshness_state: stale
stale_reason: 3 commits behind HEAD (HEAD: xyz789abc)
```

Agents read this document at session start and branch on its contents:

| Condition | Action |
|-----------|--------|
| `freshness_state == fresh` AND `pending_commits < 10` | Use cached knowledge from AgentStateBackend directly — no re-extraction needed. |
| `freshness_state == stale` OR `pending_commits >= 10` | Trigger smart reconciliation: re-extract only changed files before proceeding. |
| `index_status == partial` | Resume extraction from `component_cursor` — a prior indexing run was interrupted. |
| `index_status == failed` | Alert operator; do not use cached knowledge. |

---

## 6. Lag Computation

Lag quantifies *how far behind* Bifrost is from current source state.

### 6.1 Lag Types

| Lag type | Measures |
|----------|----------|
| **behind-remote-default** | Distance between Bifrost's indexed revision and the source's current default branch HEAD. Primary lag metric for code repos. |
| **behind-source-update** | Distance between Bifrost's indexed state and the source's latest modification. Applicable to any source type (Jira tickets, design docs, schemas). |
| **behind-runtime-correlation** | Time since Bifrost last correlated static knowledge with runtime observations. Applicable only when runtime sources are connected. |
| **behind-doc-refresh** | Time since generated docs (T3) were last rebuilt from current T2 knowledge. |

### 6.2 Lag Formula

**Revision-based (preferred for code):**
```
lag = distance(source_current_revision, bifrost_indexed_revision)
```
Where `distance` may be commit count, or when commit count is unavailable, falls back to time-based.

**Time-based (fallback, and default for non-code sources):**
```
lag = source_checked_at - indexed_at
```
When the source does not expose meaningful revisions (e.g., a Slack channel, a metrics dashboard), time-based lag is the only option.

### 6.3 Lag Severity

| Severity | Time-based threshold | Description |
|----------|---------------------|-------------|
| **acceptable** | < 1 hour | Knowledge is current enough for most tasks. |
| **stale** | 1 hour -- 24 hours | Knowledge may be outdated. Consumers should be warned. |
| **critical** | > 24 hours | Knowledge is likely outdated. Consumers must be warned prominently. |

Thresholds are **configurable per source type**. Examples:

| Source type | Acceptable | Stale | Critical |
|-------------|-----------|-------|----------|
| Code repo (default branch) | < 15 min | 15 min -- 4 hr | > 4 hr |
| Jira/work items | < 1 hr | 1 hr -- 24 hr | > 24 hr |
| Design docs | < 24 hr | 24 hr -- 7 days | > 7 days |
| Runtime observations | < 1 hr | 1 hr -- 6 hr | > 6 hr |
| Deployment state | < 15 min | 15 min -- 2 hr | > 2 hr |

### 6.4 Lag Propagation

Lag is **transitive**. When a source becomes stale, everything derived from it inherits that staleness.

**Example: Lag propagation through knowledge tiers**

```
Source: repo-webhook-api (Git)
  indexed_revision: abc123
  source_current_revision: def456
  lag: 3 hours (STALE)
      |
      v
T1 Fact: "WebhookService exposes POST /webhooks"
  derived_from: repo-webhook-api @ abc123
  status: STALE (source is stale)
      |
      v
T2 Knowledge: "Delivery flow calls WebhookService.enqueue()"
  derived_from: [T1 fact above, T1 fact from repo-dispatcher @ fresh]
  status: STALE (one contributing T1 fact is stale)
      |
      v
T3 Doc: "Delivery Flow Overview"
  derived_from: [T2 knowledge above]
  status: STALE (source T2 is stale)
```

A single stale source propagates through the entire chain. The staleness severity at each tier is the worst severity among its inputs.

---

## 7. Confidence Model

Confidence declares how trustworthy a piece of knowledge is, independent of whether a consumer agrees with it.

### 7.1 Confidence Levels

> **This is the authoritative ConfidenceLevel definition for all Bifrost specs.**
> Other specs (S1, S5, S6, S8, S9) reference this definition without re-defining it.
> Any other definition of ConfidenceLevel found in those specs is an error to be corrected.

```python
# CANONICAL CONFIDENCELEVEL DEFINITION
# All Bifrost specs must use this definition from S3.
# Do not redefine ConfidenceLevel in any other spec.

ConfidenceLevel = Literal['confirmed', 'inferred', 'stale', 'unknown', 'conflicting']

CONFIDENCE_SEMANTICS: dict[ConfidenceLevel, str] = {
    'confirmed':    'Multiple independent sources agree; recently verified against current source revision',
    'inferred':     'Logically derived from confirmed facts; not directly observed in source',
    'stale':        'Was confirmed or inferred; source has since changed — needs re-verification',
    'unknown':      'No information available; entity exists but knowledge not yet extracted',
    'conflicting':  'Two or more sources provide contradictory information — both surfaced to user',
}

# Numeric confidence score (supplementary)
# Stored in PostgreSQL alongside the ConfidenceLevel enum
# Used for ranking and prioritization, not for user-facing display
ConfidenceScore = float  # 0.0 - 1.0
CONFIDENCE_SCORE_THRESHOLDS: dict[str, float] = {
    'confirmed':  0.85,  # score >= 0.85 → display as confirmed
    'inferred':   0.60,  # score 0.60-0.84 → display as inferred
    'stale':      0.30,  # score 0.30-0.59 after invalidation → display as stale
    'unknown':    0.0,   # score < 0.30 → display as unknown
    # conflicting: computed from source disagreement, not score threshold
}
```

| Level | Meaning | Typical scenario |
|-------|---------|-----------------|
| **confirmed** | Multiple independent sources agree; recently verified against current source revision. | Fact extracted from current code, verified by fresh index. |
| **inferred** | Logically derived from confirmed facts; not directly observed in source. | Dependency inferred from import analysis + API call patterns. |
| **stale** | Was confirmed or inferred; source has since changed — needs re-verification. | Fact was confirmed at commit abc123, source is now at def456. |
| **unknown** | No information available; entity exists but knowledge not yet extracted. | Asked about a service with no connected source. |
| **conflicting** | Two or more sources provide contradictory information — both surfaced to user. | Code says service A calls B; runtime traces show A calls C. |

### 7.2 Confidence Score

Each knowledge artifact carries both a categorical level and a numeric score:

```python
class Confidence(BaseModel):
    level: ConfidenceLevel  # canonical type from S3 — the primary user-facing representation
    score: float            # 0.0 to 1.0 — supplementary, stored in PostgreSQL for ranking
    basis: list[ConfidenceBasis]
    degraded_at: datetime | None = None    # when confidence last dropped
    degraded_from: ConfidenceLevel | None = None  # what level it dropped from

class ConfidenceBasis(BaseModel):
    source_id: str
    source_type: SourceType
    evidence_type: Literal['direct', 'inferred', 'corroborating']
    freshness: SourceFreshness
```

**Score mapping (defaults):**

| Level | Score range |
|-------|------------|
| confirmed | 0.85 -- 1.0 |
| inferred | 0.60 -- 0.84 |
| stale | 0.30 -- 0.59 (after invalidation) |
| conflicting | computed from source disagreement, not score threshold |
| unknown | 0.0 -- 0.29 |

The exact score within a range may vary based on the number and quality of contributing sources. The numeric score is supplementary — the `ConfidenceLevel` enum is what users and UI surfaces display.

### 7.3 Confidence Degradation Rules

Confidence decays over time via a **step function**, not linear decay.

```
confirmed ──[source changes or TTL expires]──> stale
inferred  ──[source changes or TTL expires]──> stale
stale     ──[re-extraction confirms]─────────> confirmed or inferred
stale     ──[critical lag threshold]─────────> unknown (effectively: "we no longer trust this")
```

**This is a step function.** A fact is confirmed at 1.0 until the moment its source changes or its TTL expires, at which point it drops to stale. There is no gradual linear decay from 1.0 to 0.0 over time. The rationale: linear decay implies false precision. Either we have fresh evidence or we do not.

**TTL is configurable per source type:**

| Source type | Default TTL before confirmed -> stale |
|-------------|--------------------------------------|
| Code repo | Until next commit to relevant paths |
| Jira/work items | 24 hours |
| Design docs | 7 days |
| Runtime observations | 6 hours |
| Config/schema | Until next change event |

### 7.4 Confidence Composition

When multiple sources contribute to a single piece of knowledge, their confidences must be composed.

**V1 rule (simple):**

```
if all sources agree:
    composite_confidence = min(individual_confidences)
    composite_level = worst(individual_levels)
if any sources disagree:
    composite_level = 'conflicting'
    composite_confidence = max(0.1, min(individual_confidences) * 0.5)
```

The `min` rule when agreeing is deliberately conservative: the composite is only as strong as the weakest contributing source. This prevents a single strong source from masking a stale or weak one.

**Confidence computation flowchart:**

```
Input: Set of (source, fact, confidence) tuples for a knowledge item

  [Collect all contributing source confidences]
                    |
                    v
        [Do all sources agree on the fact?]
           /                    \
         YES                    NO
          |                      |
          v                      v
  [composite = min(scores)]   [level = 'conflicting']
  [level = worst(levels)]     [score = min(scores) * 0.5]
          |                      |
          v                      v
  [Attach basis list]       [Attach basis list +
                             conflict record]
          |                      |
          v                      v
          [Return Confidence]
```

---

## 8. Conflict Detection

Conflicts are first-class objects in Bifrost. They are never silently resolved.

### 8.1 Conflict Types

| Type | Description | Example |
|------|-------------|---------|
| **source-source** | Two sources assert different facts about the same entity. | Git repo says service uses PostgreSQL; service catalog says MySQL. |
| **stale-current** | Cached/indexed knowledge contradicts fresh source data. | Indexed: endpoint `/v1/webhooks`; fresh source: endpoint renamed to `/v1/events`. |
| **static-runtime** | Code analysis and runtime observation disagree. | Static analysis says A calls B; traces show A calls C. |
| **intent-implementation** | Design doc says X, code does Y. | ADR says "use event sourcing"; implementation uses direct DB writes. |
| **dependency-disagreement** | A declares dependency on B, but B has no knowledge of A. Or B's interface does not match what A expects. | Service A imports client for B v2; B's latest is v3 with breaking changes. |

### 8.2 Conflict Representation

```python
class Conflict(BaseModel):
    conflict_id: str
    type: Literal['source-source', 'stale-current', 'static-runtime',
                  'intent-implementation', 'dependency-disagreement']
    artifacts: list[ConflictArtifact]    # the specific knowledge items in disagreement
    description: str                     # human-readable explanation
    severity: Literal['info', 'warning', 'critical']
    detected_at: datetime
    resolution_hint: str | None = None   # optional guidance for resolving
    resolved_at: datetime | None = None
    resolved_by: Literal['source-update', 'manual', 'refresh'] | None = None

class ConflictArtifact(BaseModel):
    artifact_id: str
    artifact_type: Literal['source_fact', 'knowledge_item', 'doc_section']
    source_id: str
    asserted_value: str           # what this artifact claims
    confidence: Confidence
    freshness: SourceFreshness
```

### 8.3 Conflict Severity Rules

| Severity | When |
|----------|------|
| **info** | Minor disagreement, low-impact entities, or one source is clearly stale. |
| **warning** | Meaningful disagreement on entities used in active flows or dependencies. |
| **critical** | Disagreement on interfaces, contracts, or dependencies actively relied upon, where acting on wrong information causes breakage. |

### 8.4 Conflict in AgentStateBackend Documents

If two agents write different content to the same entity document concurrently (e.g., two ExtractionAgent sessions both analyzing the same changed component):

**Track A — PostgreSQL `knowledge_text_cache`:**

Last-writer-wins by default (UPSERT). To prevent silent overwrites, writers use a content-hash check:

```sql
UPDATE knowledge_text_cache
   SET content = $new_content, content_hash = $new_hash
 WHERE path = $path AND content_hash = $expected_hash
```

If 0 rows affected: another writer won the race. Re-read the current content, merge the two versions, then retry the write.

**Track B — Anthropic Memory Stores:**

Use SHA256 preconditions on memory store writes:

```
POST /memory_stores/{id}/memories
  with precondition: { type: "content_sha256", content_sha256: $expected }
```

If `409 Conflict` is returned: another writer won. Re-read, merge, retry.

**Merge strategy for conflicting extractions:**

| Conflict type | Resolution |
|---------------|------------|
| Same field, different values | Elevate to `conflicting` confidence; surface both versions to user via the Conflict representation (section 8.2). |
| Different fields (each agent extracted distinct aspects) | Merge without conflict — combine the non-overlapping fields into the document. |
| One version is `confirmed`, other is `inferred` (same field, same value) | Keep `confirmed` version; log that `inferred` was discarded as redundant. |
| One version is `confirmed`, other is `inferred` (same field, different values) | Keep `confirmed` value; log discarded `inferred` value; treat as `conflicting` for review. |

---

## 9. Invalidation Model

Invalidation determines what must be recomputed when upstream state changes.

### 9.1 Invalidation Triggers

| Trigger | Source | Behavior |
|---------|--------|----------|
| **Source change event** | Webhook, push event, modification notification | Mark affected T1 facts stale, propagate to T2 and T3. |
| **Scheduled check** | Cron/scheduler | Compare indexed revision to current revision; if different, mark stale. |
| **Manual refresh** | User or agent request | Force re-extraction for specified scope. |
| **Task-scoped refresh** | Orchestrator determines freshness insufficient for current task | Refresh specific sources needed for the task before proceeding. |

### 9.2 Invalidation Propagation

Invalidation follows the dependency graph from source through tiers:

```
Source change detected for source S
    |
    v
Find all T1 facts derived from S
    |
    +--> Mark each T1 fact as stale
    |        |
    |        v
    |    Find all T2 knowledge items that used this T1 fact
    |        |
    |        +--> Mark each T2 item as stale
    |        |        |
    |        |        v
    |        |    Find all T3 docs that used this T2 item
    |        |        |
    |        |        +--> Mark each T3 doc as stale
```

This is a dependency graph traversal. The system must maintain a mapping from each knowledge artifact to the artifacts that derived from it (a reverse dependency index).

### 9.3 Invalidation Modes

| Mode | Behavior | When to use |
|------|----------|-------------|
| **mark-stale** (async) | Set freshness state to stale. Recomputation happens later, on next access or scheduled job. | Default for all background invalidation (V1). |
| **immediate-refresh** (sync) | Block until the stale artifact is re-extracted from the fresh source and recomputed. | Explicit user/agent refresh request, or task-scoped freshness requirement. |

**V1 default: mark-stale.** Immediate-refresh is available on explicit request only.

### 9.4 Selective Invalidation

Invalidation can be scoped to:

| Scope | Effect |
|-------|--------|
| **By source** | Invalidate all T1 facts from a specific source object. |
| **By repo** | Invalidate all T1 facts across all sources for a repo. |
| **By knowledge tier** | Invalidate all T2 items (recompute derived knowledge) without re-extracting T1. |
| **By entity type** | Invalidate all facts of a specific type (e.g., all dependency edges). |
| **By domain** | Invalidate all knowledge scoped to a domain. |

### 9.5 Invalidation Procedure (Dual-Track)

When an invalidation event occurs (webhook push, manual trigger, or scheduled reconciliation):

**Track A (PostgreSQL + knowledge_text_cache):**

1. `UPDATE repo_freshness SET freshness_state = 'stale', stale_reason = $reason, pending_commits = $count` for the affected repo.
2. `UPDATE knowledge_text_cache SET content = REPLACE(content, 'freshness: fresh', 'freshness: stale') WHERE repo_id = $repo_id AND entity_type IN ('component', 'flow')` — marks individual entity documents as stale so agents reading them directly see current state.
3. Publish invalidation event to internal event bus so agents currently running in a session are notified and can decide whether to re-extract before proceeding.

**Track B (PostgreSQL + Anthropic Memory Stores):**

1. `UPDATE repo_freshness` in PostgreSQL (same as Track A step 1).
2. Write updated freshness state document to Memory Store: PATCH `/repo/{id}/freshness/state.md` with `freshness_state: stale` and updated `stale_reason` and `pending_commits`.
3. Cascade: agents that next read the freshness document at session start will detect staleness and trigger smart reconciliation automatically. No active notification needed — the document acts as the signal.

---

## 10. Update Triggers

The following events trigger freshness checks or invalidation:

| Trigger | Source | Action |
|---------|--------|--------|
| **Webhook event** | Git push, PR merge, Jira update, doc edit | Targeted invalidation of affected sources. |
| **Scheduled reconciliation** | Cron job | Full comparison of indexed vs current revision for all sources. Catch anything webhooks missed. |
| **User refresh request** | CLI, UI, API | Immediate-refresh for requested scope. |
| **Task-driven freshness requirement** | Orchestrator | Before answering a query, check freshness of required sources. If stale beyond acceptable threshold for the task, trigger refresh before proceeding. |
| **Branch change** | User switches context to a different branch | Load branch-specific index if available, or mark knowledge as branch-unverified. |

---

## 11. Local vs Remote

### 11.1 Default Behavior

Bifrost answers from **remote source state** by default. The remote default branch (typically `main`) is the baseline truth.

### 11.2 Local Workspace Inclusion

When a user includes local workspace state (uncommitted changes, local branch):

1. Local state is indexed separately and tagged as `scope: local`.
2. All knowledge derived from local state carries a `local_divergence` flag.
3. The system computes and shows divergence: what differs between local and remote.
4. Local and remote knowledge are **never silently mixed**. If an answer combines both, each component is labeled.

### 11.3 Local Freshness

Local workspace freshness is inherently ephemeral. It is:
- Fresh at the moment of indexing.
- Immediately stale if the user modifies files without re-indexing.
- Always shown with a "local, as of {timestamp}" qualifier.

---

## 11.5 Smart Reconciliation (Lag-Based Re-extraction)

When an agent detects staleness at session start (via the freshness state document at `/repo/{id}/freshness/state.md`):

1. **Read freshness state:** obtain `last_commit_indexed` from the freshness document.
2. **Fetch changed files:** `git log {last_commit_indexed}..HEAD --name-only` to identify what changed since the last index.
3. **Map changed files to affected components:**
   ```sql
   SELECT entity_id FROM entity_file_mappings WHERE file_path = ANY($changed_files)
   ```
4. **Re-extract only affected components** — not a full repo re-index. The ExtractionAgent processes only the files and entities that changed.
5. **For each re-extracted component:**
   a. Write updated entity to PostgreSQL (`UPDATE entities SET ...`).
   b. Write updated text document to AgentStateBackend (UPSERT at the component path, e.g., `/repo/{id}/components/{name}.md`).
   c. Set confidence to `inferred` immediately after re-extraction. Promote to `confirmed` once cross-validated against at least one additional source.
6. **Update freshness state:**
   a. Set `last_commit_indexed` to current HEAD.
   b. Set `freshness_state` to `fresh`.
   c. Set `pending_commits` to `0`.
   d. Write updated freshness document to AgentStateBackend.

This is the "smart recon strategy based on lag" from the Bifrost architecture vision. It keeps re-extraction cost proportional to what actually changed, not the size of the full repo.

**Full re-extraction (cold start) is only needed when:**
- First-time indexing of a repo (no prior index exists).
- The `component_cursor` or extraction state has been lost or corrupted.
- Structural changes affect more than 50% of components (heuristic threshold; configurable).

---

## 11.6 Track B Managed Agent Limitations and Freshness

The following sections address constraints specific to Track B (Anthropic Memory Stores + managed agent sessions) that do not apply to Track A.

> **Context:** Track B runs knowledge extraction and reconciliation as managed agent sessions via the Anthropic Agents API. These sessions are ephemeral (L2), cannot communicate directly with each other (L6), and multiple sessions may run concurrently against the same repo (L7). Freshness state must therefore live in the Memory Store — never in process-local memory.

#### Track B Freshness State Storage

Memory Store path for freshness: `/repo/{id}/freshness/state.md`

Document format:

```markdown
---
repo_id: {id}
freshness_state: fresh | stale | indexing | error
last_indexed_at: 2025-01-15T10:00:00Z
last_commit_sha: abc123
file_count: 247
entity_counts:
  components: 45
  flows: 12
  dependencies: 87
stale_reason: null | "new_commits" | "extraction_failed" | "checkpoint_incomplete"
---
```

All agent sessions read this document at startup to determine whether extraction or reconciliation is needed. Because sessions are ephemeral, no in-process state is trusted across session boundaries — the Memory Store document is the single source of truth for freshness.

#### Track B Freshness Concurrency Safety

**Problem:** Two `TaskCoordinator` sessions may simultaneously detect a repo as stale and both trigger re-extraction, wasting compute and risking conflicting writes to entity documents.

**Solution:** Optimistic locking via a short-lived extraction lock document in the Memory Store.

```python
async def claim_extraction_lock(
    state_backend: AgentStateBackend,
    repo_id: str,
    current_session_id: str,
) -> dict:
    """Before starting extraction, claim the freshness lock."""
    lock_path = f"/repo/{repo_id}/freshness/extraction-lock.md"
    existing = await state_backend.read(lock_path)

    if existing and existing["locked_until"] > datetime.now(UTC).timestamp():
        return {"claimed": False, "locked_by_session": existing["session_id"]}

    # Attempt to claim lock (15 min TTL = max session duration)
    await state_backend.write(lock_path, {
        "session_id": current_session_id,
        "locked_at": datetime.now(UTC).isoformat(),
        "locked_until": datetime.now(UTC).timestamp() + 900,  # 15 minutes
    })

    # Verify we won the race (re-read and check our session_id)
    lock_check = await state_backend.read(lock_path)
    return {"claimed": lock_check and lock_check["session_id"] == current_session_id}
```

**If lock claim fails:** the requesting session does not error. Instead it polls the Memory Store for `freshness_state === 'fresh'` on the repo's state document, waiting up to 5 minutes with a 10-second polling interval. When the winning session completes extraction and writes `freshness_state: fresh`, all waiting sessions proceed using the freshly written knowledge.

**Lock release:** the winning extraction session writes `freshness_state: fresh` (or `error`) to `state.md` and deletes `extraction-lock.md` on completion. The TTL on the lock document acts as a safety valve if the session crashes before explicit release.

#### Track B Indexing Timeout Check

In Track B, the `indexing` freshness state also implies "a managed agent session is currently running extraction." Because managed sessions are ephemeral, a session crash leaves no cleanup hook — the `freshness_state` will remain `indexing` indefinitely until another session observes it.

**`indexing_timeout_check` rule:** if `freshness_state === 'indexing'` AND `last_indexed_at` is more than **20 minutes** ago, treat the state as `stale` and allow re-extraction to proceed. Any session evaluating freshness MUST apply this check before deciding whether to skip extraction.

```python
def resolve_effective_freshness_state(doc: FreshnessStateDocument) -> EffectiveFreshnessState:
    if (
        doc.freshness_state == "indexing"
        and (datetime.now(UTC) - datetime.fromisoformat(doc.last_indexed_at)).total_seconds() > 20 * 60
    ):
        # Managed session likely crashed without cleanup — treat as stale
        return "stale"
    return doc.freshness_state
```

The 20-minute threshold matches the maximum expected extraction session duration for a mid-size repo. It is configurable via `BIFROST_INDEXING_TIMEOUT_MS` environment variable.

---

## 12. Interface Contracts

### 12.1 Freshness Query API

```python
# Get freshness for a specific scope
# GET /api/v1/freshness?scope=repo&id={repo_id}
# GET /api/v1/freshness?scope=domain&id={domain_id}
# GET /api/v1/freshness?scope=source&id={source_id}

class FreshnessResponse(BaseModel):
    scope: ScopeDescriptor
    overall_freshness: FreshnessState     # worst across children
    overall_lag: LagSeverity
    children: list[FreshnessDetail]       # per-source or per-repo breakdown
    checked_at: datetime
```

### 12.2 Lag Dashboard Data

```python
# Aggregate lag across all tracked sources
# GET /api/v1/lag/summary

class LagSummary(BaseModel):
    total_sources: int
    fresh_count: int
    stale_count: int
    critical_count: int
    unknown_count: int
    worst_lag: WorstLag  # source_id, lag_seconds, severity
    by_source_type: dict[SourceType, LagBreakdown]
    by_domain: dict[str, LagBreakdown]
```

### 12.3 Invalidation Event Emissions

When invalidation occurs, the system emits events for downstream consumers:

```python
class InvalidationEvent(BaseModel):
    event_id: str
    trigger: InvalidationTrigger
    scope: ScopeDescriptor
    affected_artifacts: list[str]       # IDs of artifacts marked stale
    affected_tiers: list[Literal['T1', 'T2', 'T3']]
    timestamp: datetime
```

### 12.4 Refresh Request API

```python
# Request refresh for a scope
# POST /api/v1/refresh
# Body:
class RefreshRequest(BaseModel):
    scope: ScopeDescriptor
    mode: Literal['mark-stale', 'immediate']
    reason: str
    requested_by: str

class RefreshResponse(BaseModel):
    request_id: str
    status: Literal['accepted', 'in_progress', 'completed', 'failed']
    affected_sources: int
    estimated_completion: datetime | None = None  # for immediate mode
```

---

## 13. Dependencies

| Dependency | What this spec needs from it |
|------------|------------------------------|
| **S1 — Knowledge Model** | Defines what artifacts exist (T1, T2, T3), their relationships, and their identifiers. Freshness and confidence attach to these artifacts. |
| **S2 — Source Adapters** | Provides revision tracking per source type. Each adapter must expose `current_revision()` and `last_modified()` for freshness comparison. |
| **S4 — Storage** | Stores freshness metadata, confidence records, conflict records. Must support efficient queries by scope and staleness. |
| **S5 — Orchestration** | Consumes freshness data to decide whether to refresh before answering a query. Emits task-scoped refresh requests. |

---

## 14. Key Decisions

### 14.1 Timestamp-based freshness for V1

V1 uses timestamp-based freshness comparison: `indexed_at` vs `source_checked_at`, supplemented by revision string equality checks where available.

**Deferred:** Version vectors for concurrent multi-source freshness tracking. These are needed only when Bifrost tracks multiple parallel branches or multiple writers to the same source simultaneously.

### 14.2 Step-function confidence decay, not linear

Confidence drops in discrete steps (confirmed -> stale -> unknown), not as a continuous function of time. Rationale: a linearly decaying score (e.g., "0.73 confidence") implies false precision about how much to trust stale data. In reality, you either have fresh evidence or you do not.

### 14.3 Async mark-stale invalidation by default

V1 defaults to mark-stale (async) invalidation. Artifacts are flagged stale and recomputed lazily — on next access or on the next scheduled reconciliation pass. Synchronous immediate-refresh is available only on explicit request.

Rationale: eager recomputation of all transitive dependents on every source change would create unsustainable load. Most stale artifacts are never accessed before the next scheduled recomputation anyway.

### 14.4 Transitive staleness via dependency graph traversal

When a source is marked stale, all T1 facts from that source are marked stale, all T2 knowledge using those T1 facts is marked stale, and all T3 docs using that T2 knowledge is marked stale. This requires maintaining a reverse dependency index from each artifact to its dependents.

### 14.5 Conservative composition (min-confidence)

V1 uses `min(confidences)` when sources agree and `conflicting` when they disagree. This is deliberately conservative — it ensures consumers are never over-confident. More sophisticated composition (weighted, Bayesian) is deferred.

---

## 15. V1 Scope

### Included in V1

- Timestamp-based freshness tracking for all indexed sources.
- Revision string comparison where source adapters provide it (Git SHA, ETag).
- Lag computation (time-based) with configurable severity thresholds.
- Confidence levels: confirmed, inferred, stale, unknown, conflicting.
- Simple min-confidence composition.
- Step-function confidence degradation.
- Mark-stale async invalidation as default.
- Immediate-refresh on explicit request.
- Conflict detection for source-source and stale-current types.
- Freshness query API and refresh request API.
- Per-repo and per-domain freshness rollups.

### Deferred beyond V1

- **Bayesian confidence composition.** Weighted scoring based on source authority and historical accuracy.
- **Automatic transitive propagation at scale.** V1 propagates staleness through the dependency graph, but does not automatically trigger recomputation of all transitive dependents. Recomputation is lazy.
- **Runtime correlation freshness.** Tracking freshness of runtime-derived knowledge requires runtime source adapters not yet built.
- **Version vectors.** Needed for multi-branch, multi-writer freshness tracking.
- **Conflict auto-resolution.** V1 surfaces conflicts. Resolution is manual or via source update.
- **Static-runtime and intent-implementation conflict detection.** Requires cross-tier comparison logic not yet built.
- **Confidence history and trending.** Tracking how confidence for a knowledge area changes over time.

---

## 16. Future Evolution

1. **Bayesian confidence composition (V2).** Replace min-confidence with a model that weights sources by authority, historical accuracy, and recency. A source that has been consistently correct deserves higher weight.

2. **Proactive freshness (V2).** Instead of only checking freshness on access, proactively refresh high-value knowledge areas on a priority schedule. Prioritization based on access frequency, criticality, and staleness risk.

3. **Runtime correlation freshness (V2+).** When runtime source adapters are available, maintain a separate freshness dimension for runtime-derived knowledge. Detect static-runtime divergence as a first-class conflict type.

4. **Version vectors (V3).** For organizations with complex branching strategies or multiple parallel knowledge streams, replace timestamp comparison with version vectors that correctly track concurrent evolution.

5. **Confidence trending and alerting.** Track confidence over time for important knowledge areas. Alert when confidence in a critical area (e.g., a high-traffic flow) drops below a threshold.

6. **Automatic conflict resolution policies.** Allow organizations to define policies like "when code and design doc conflict, code wins" or "when runtime and static conflict, runtime wins for dependency edges."

---

## 17. Open Questions

1. **Granularity of reverse dependency index.** Should the reverse dependency index track at the individual fact level (expensive to maintain, precise invalidation) or at the source-to-knowledge-item level (cheaper, broader invalidation)? V1 likely starts with source-level and refines later.

2. **TTL values.** The default TTLs proposed in this spec are starting points. What is the right process for tuning them? Should they be per-organization configurable from day one, or hardcoded defaults with override capability?

3. **Freshness cost budget.** How much compute/API budget should freshness checking consume? Scheduled reconciliation for hundreds of sources could be expensive. Should there be a priority queue (check high-value sources more frequently)?

4. **Conflict severity escalation.** When a conflict persists unresolved for an extended period, should its severity automatically escalate? If so, what are the escalation thresholds?

5. **Branch-aware freshness.** When a user is working on a feature branch, should Bifrost track freshness against that branch's HEAD, or always against the default branch? Both have valid use cases. How should the UX handle this?

6. **Partial staleness.** If a source has 100 files and 1 changed, is the entire source stale or only the affected T1 facts? V1 marks the source stale at the source level. Fine-grained path-level staleness tracking would be more efficient but more complex.

7. **Confidence floor.** Should there be a minimum confidence below which Bifrost refuses to present knowledge at all (rather than presenting it with a low-confidence warning)? Or should the consumer always decide?
