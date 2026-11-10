# 09 — Glossary

Domain terms used by the Bifrost codebase, schema, and other handbook chapters. Where a term has a canonical definition elsewhere (F1 spec, the collaboration feature docs, a model docstring), that source is linked. Where the term is used in multiple contexts with subtly different meanings, the differences are surfaced here rather than glossed over.

> **TL;DR.** Roughly 80 entries, alphabetised. Use the cluster index below to navigate by category. Terms with overloaded meanings (Brief, Task, Tier, Dependency, Artifact) spell out each meaning on a separate numbered line. Cross-references hyperlink within this document and out to the data-model / architecture chapters.

> **Read this when** you hit a term in the codebase or another handbook chapter and want a one-line definition with a pointer to the canonical source.
> **Skip if** you already know the term — this is a reference, not a tutorial.

---

## Cluster index

Jump to a thematic group. Within each cluster terms remain alphabetised, but the cluster header points you to the relevant section of the canonical alphabetical entries below.

### Concepts (the model)
[Action layer](#a) · [Belief DAG](#b) · [Bifrost](#b) · [Brief](#b) · [Component](#c) · [Confidence](#c) · [Confidence level](#c) · [Dependency](#d) · [Domain](#d) · [Entity](#e) · [Entity tier](#e) · [Flow](#f) · [Knowledge tier](#k) · [Region](#r) · [Repo](#r) · [Scope kind](#s) · [Scope value](#s) · [URN](#u)

### Roles (agents)
[`AgentRuntime`](#a) · [`AgentStateBackend`](#a) · [Clarifier](#c) · [Confluence agent](#c) · [Coordinator](#c) · [Discussion agent](#d) · [Executor](#e) · [GitHub agent](#g) · [Intent router](#i) · [Jira agent](#j) · [Plan agent](#p) · [Repo agent](#r) · [Researcher](#r) · [Router (chat)](#r) · [Router (intent)](#r) · [Slack agent](#s) · [Synthesizer (chat)](#s) · [Synthesizer (collaboration)](#s) · [Triage](#t)

### Lifecycles (state machines)
[Archive (handover doc)](#a) · [Archive (task)](#a) · [Ask-back](#a) · [Assumption](#a) · [Checkpoint](#c) · [Clarification](#c) · [Collab session](#c) · [Doubt](#d) · [Doubt candidate](#d) · [Extraction](#e) · [Extraction phase](#e) · [Extraction trigger](#e) · [Fresh](#f) · [Freshness state](#f) · [Learning loop](#l) · [Loop-back](#l) · [Phase (extraction)](#p) · [Phase (task)](#p) · [Plan key](#p) · [Quick action](#q) · [Retracted (assumption)](#r) · [Stale](#s) · [Task](#t) · [Task event](#t) · [Verify](#v) · [Verify cmd](#v) · [Very stale](#v) · [Wave (plan)](#w) · [Wave (handover build)](#w)

### Storage (data)
[Bundle](#b) · [Catalog](#c) · [Clone (`git clone`)](#c) · [Community](#c) · [Cost log](#c) · [HNSW index](#h) · [Knowledge index](#k) · [Manifest hash](#m) · [Neutral cwd](#n) · [Org catalog](#o) · [Org repo](#o) · [Parent path (handover)](#p) · [pgvector](#p) · [Repo freshness](#r) · [Section (handover)](#s) · [`source_type`](#s) · [Source basis](#s) · [TSVECTOR](#t) · [Worktree](#w)

### Surfaces (UI / API)
[Feature handover](#f) · [Handover doc](#h) · [Handover audit](#h) · [Handover body history](#h) · [Onboarding lens](#o) · [Requirement](#r) · [Scope (chat)](#s) · [Session (chat)](#s) · [Session (collaboration)](#s) · [Session pool config](#s) · [SSE](#s)

### Tooling (integrations + safety)
[Canary](#c) · [Glossary](#g) · [Global rule](#g) · [`inherit_user_mcp`](#i) · [Intent](#i) · [KTLO](#k) · [Lifespan](#l) · [Manifest](#m) · [Matching](#m) · [MCP server](#m) · [MCP tool](#m) · [Primary repo](#p) · [Prompt safety](#p) · [Secret scanner](#s) · [Service tier](#s) · [Skim search](#s) · [Team](#t) · [Track A](#t) · [Track B](#t)

---

## A

**Action layer.** The "do" side of Bifrost — the code that turns a user intent into an end-to-end coding change. Lives in `src/bifrost/actions/`. Anchored by the four-step loop ([Discuss](#d) → [Plan](#p) → [Execute](#e) → [Verify](#v)) running inside one or more phases of a [Task](#t).
*Where you encounter it.* `/tasks/:id` page; intent router; plan agent; executor; verifier.

**`AgentRuntime`.** Abstract interface in `src/bifrost/agents/` that lets the action layer and research coordinator call into a Claude session. Track A's implementation is the `claude -p` subprocess client; Track B's implementation will be the Anthropic Managed Agents API. Higher layers should not depend on which is in use.
*Don't confuse with.* [`AgentStateBackend`](#a) — runtime is *how to call*, backend is *where memory is stored*.

**`AgentStateBackend`.** Abstract interface in `src/bifrost/storage/backend.py` for persisting an agent's working memory (intermediate text, partial reasoning). Track A backs it with `knowledge_text_cache`; Track B will back it with Anthropic Memory Stores. Drop-in.
*Don't confuse with.* [`AgentRuntime`](#a).

**Archive (handover doc).** Soft-archival — non-NULL `archived_at` column on the row, on-disk file untouched. Hidden from the default list view. Restoring is an `UPDATE archived_at = NULL`.
*Where you encounter it.* `DELETE /handover-docs/{id}`; `POST /handover-docs/{id}/restore`.

**Archive (task).** Same pattern — `archived_at` column on `tasks`, soft-archive only, swept hourly by `auto_archive`.

**Artifact.** Two senses:
1. **Handover artifact** — the on-disk Markdown (`docs/handovers/.../index.md`).
2. **Task artifact** — a row in `task_artifacts`, one of five canonical kinds (`context`, `research`, `plan`, `summary`, `verify`) holding the body Markdown produced by an agent in a phase of a Task.

**Ask-back.** The action layer's term for an agent question to the user. One row per ask-back in `ask_backs`, with the `answered_from` field classifying how it was resolved (graph alone, research pipeline, extraction-then-research, user, or budget-exceeded).
*Don't confuse with.* [Doubt](#d) (collaboration) or [Clarification](#c) (action layer); ask-back is the broader umbrella for any agent-emitted question.

**Assumption.** An atomic belief cell within a task artifact. Has a provenance grade (`source_type`, `confidence`, `tier`), a lifecycle state (`active` or `retracted`), and edges into the artifacts that use it (the [belief DAG](#b)). See [03 §7 Action layer](./03-data-model.md#7-action-layer).

---

## B

**Belief DAG.** The directed acyclic graph formed by `task_artifacts` + `task_assumptions` + `task_assumption_uses`. Lets the system invalidate downstream artifacts cleanly when an assumption is retracted.

**Bifrost.** The platform itself. The naming refers to the bridge between the structured world of code and the unstructured world of human discussion — the rainbow bridge in Norse mythology. Every external system (Jira, Slack, Confluence, GitHub) is one footing of the bridge; the agent runtime is the span.

**Brief.** Two senses (commonly conflated — be careful):
1. **Action layer (legacy).** Pre-rename name for what is now a [Task](#t). Every `brief_*` table and column was renamed to `task_*`. The codebase no longer uses this term; it persists only in older history.
2. **Collaboration.** The final artifact a collaboration session produces — a Markdown document the requester can take to the owning team. Stored in `collab_artifacts` with `kind=final_brief`. Source: `src/bifrost/collaboration/synthesizer.py`.

*Don't confuse with.* This is the most overloaded term in Bifrost. When in doubt, qualify as "session brief" or refer to "task" instead of "brief" for the action layer.

**Bundle.** Short for `ResearchBundle`. The cached output of a chat research turn — the list of per-source reports plus entity snippets — persisted to `conversation_research_state` so a follow-up turn does not have to re-research from scratch.

---

## C

**Canary.** A known token injected into the user message before an LLM call, then searched for in the *parsed* output. If the canary echoes back inside the structured JSON, the system flags possible prompt injection. See `src/bifrost/extraction/cli_client.py` and `src/bifrost/extraction/prompts.py:build_canary` / `validate_canary`. → See [06 §12](./06-extraction-pipeline.md#12-prompt-safety--canary-tokens).

**Catalog.** The org-wide repo catalog populated by `POST /catalog/sync` paging `gh api orgs/<org>/repos` for each org in `sources.github.orgs`. Lives in `org_repos`. Orthogonal to `repos` (which holds *registered* repos the operator has explicitly opted into).
*Don't confuse with.* [Repo](#r) — catalog is the universe of all repos in the org; `repos` is the subset the operator has registered for extraction.

**Checkpoint.** An extraction's mid-pipeline cursor. Persisted to `repo_freshness.checkpoint_phase` + `checkpoint_file_cursor`. Lets a partial extraction resume mid-pipeline after a crash. The on-disk `ExtractionCheckpoint` dataclass in `src/bifrost/extraction/checkpoint.py` is the in-memory shape. → See [06 §9](./06-extraction-pipeline.md#9-resumability--extractioncheckpoint).

**Clarification.** An action-layer Q&A row in `task_clarifications`. Distinct from a [doubt](#d) (collaboration sessions) — same idea, different table because the action layer's clarification model is older and richer (multi-choice options, blocking-state semantics).
*Don't confuse with.* [Doubt](#d).

**Clarifier.** The agent in `src/bifrost/collaboration/clarifier.py` that runs after each `requester` turn in a collaboration session. Asks 1-3 specific clarifying questions OR signals `requirement_is_concrete=True` to advance the session. → See [07 §1](./07-collaboration-system.md#1-a-realistic-session-end-to-end).

**Clone (`git clone`).** The operator's local working tree of a registered repo. `repos.local_clone_path` records its location. Extraction reads from this. A "fresh clone" means the working tree's HEAD matches the remote's default branch.

**Collab session.** Short for `CollaborationSession`. The stateful collaboration interaction in `collaboration_sessions` + `collab_messages` + `collab_doubts` + `collab_artifacts`. → See [07](./07-collaboration-system.md).

**Community.** A Louvain-clustered group of entities, with an LLM-generated summary and a centroid embedding. Lives in `entity_communities`. Powers domain-scoped chat queries that under-retrieve when ranking individual entities.

**Component.** A type of entity — a unit of code (class / module / package) extracted in [Phase D](#p) of the pipeline. URN form `bifrost:repo:foo/bar:component:auth.controller`.

**Confidence.** A float in [0.0, 1.0] on every entity, relationship, and assumption. Stored alongside a `confidence_level` enum.
*Don't confuse with.* [Confidence level](#c) — confidence is the float, level is the enum bucket.

**Confidence level.** Enum on entities and relationships: `confirmed` / `inferred` / `stale` / `uncertain` / `unknown` / `conflicting`. Authoritative definition in S3.

**Confluence agent.** A research sub-agent (`src/bifrost/research/confluence_agent.py`) that wraps the Atlassian MCP server via `claude -p` calls with `inherit_user_mcp=True`. Reads the cache (`confluence_pages`) first, falls back to live MCP only on miss.

**Coordinator.** The chat-side orchestrator (`src/bifrost/research/coordinator.py:ResearchCoordinator`) that runs scope-resolution → router → fan-out → synthesis for a chat turn.

**Cost log.** The `llm_cost_log` table. One row per LLM call. → See [10 §4.2](./10-operations.md#42-the-source-table).

---

## D

**DAG (belief).** See *[Belief DAG](#b)*.

**Dependency.** Two senses:
1. A type of entity in the knowledge graph — extracted in [Phase F](#p) of the pipeline.
2. A row in `entity_relationships` with `rel_type` indicating an inter-component relationship.

**Discussion agent.** The action-layer agent in `src/bifrost/actions/discussion_agent.py` that drives the *Discuss* step of a phase — gathers context, surfaces ambiguities, produces a context artifact for the planner to consume.

**Domain.** A logical grouping of repos. Each repo belongs to at most one domain (`repos.domain_id`). Examples: "delivery", "platform".

**Doubt.** A structured open question in a collaboration session. Has a status: `open` / `answered_by_doc` / `answered_by_human` / `unresolved` / `escalated`. See `collab_doubts`.
*Don't confuse with.* [Clarification](#c) (action layer) or [Ask-back](#a) (umbrella term).

**Doubt candidate.** A doubt that recurs across many sessions, surfaced by the loop-back analyser as a candidate update to the parent handover doc. Spec defines a `handover_doubt_candidates` table; [needs verification] for whether it is currently provisioned in alembic.

---

## E

**Entity.** A node in the knowledge graph. Polymorphic — `entity_type` distinguishes [Component](#c) / [Flow](#f) / [Dependency](#d) / etc. URN-keyed.

**Entity tier.** A 0-3 small-int on every entity classifying knowledge breadth (broad-to-deep). Tier 0 is a lightweight pointer (mapping); tier 3 is a deeply-extracted full entity. Authoritative definition in F1 §7.1.
*Don't confuse with.* [Tier](#t) — that entry disambiguates the action-layer "assumption tier" (1-5 provenance grade) from this one (0-3 knowledge breadth).

**Executor.** The action-layer agent in `src/bifrost/actions/executor.py` that drives the *Execute* step of a phase — runs each `task_plans` row's `action_body` in the worktree.

**Extraction.** The process of running the 7-phase pipeline against a repo to populate the knowledge graph. One row per run in `extraction_tasks`. → See [06](./06-extraction-pipeline.md).

**Extraction phase.** One of A → I (with B and C handled by the GitAdapter outside the pipeline). Phases D, E, F, G, H, I run sequentially with intermediate persistence between phases.
*Don't confuse with.* [Phase (task)](#p) — which is an action-layer concept.

**Extraction trigger.** A row in `extraction_triggers` recording why a particular extraction was launched. `trigger_kind`: `auto_manifest_only` / `ask_back_gap` / `user_requested` / `scheduled`.

---

## F

**Feature handover.** A handover doc with `scope_kind=feature`. May be ephemeral (generated inside a collaboration session and not promoted to disk) or persistent. Distinct from a repo handover in being *request-shaped* rather than *artifact-shaped*. See [07](./07-collaboration-system.md).

**Fresh.** A row's `freshness_state=fresh`. Means the row's source has not changed since the last refresh. The complementary states are `stale` (source has changed) and `unknown` (we do not know).

**Flow.** A type of entity — a sequence of operations through code (an HTTP handler's call graph, a job's lifecycle). Extracted in [Phase E](#p) of the pipeline. Has segments (sub-flows). Authoritative model in S5.

**Freshness state.** Enum on `repos`, `entities`, `entity_relationships`, and `handover_docs`. Values vary slightly by table:
- `repos` / entities: `fresh` / `stale` / `unknown`.
- `handover_docs`: `fresh` / `stale` / `very_stale` / `enriched_by_children` / `stale_due_to_children`.

---

## G

**GitHub agent.** A research sub-agent (`src/bifrost/research/github_agent.py`) wrapping the GitHub MCP server. Surfaces issues, PRs, and discussions for a repo.

**Global rule.** A behavioural rule injected into agent system prompts. Stored in `global_rules`. `applies_to` is a Postgres `text[]` of agent kinds.

**Glossary.** This document.

---

## H

**Handover doc.** A durable Markdown artifact about a scope (feature / repo / domain / region / team / session). Source of truth is the on-disk `index.md`; the queryable form is the `handover_docs` row. → See [07](./07-collaboration-system.md).

**Handover audit.** A row in `handover_audits`. Append-only lifecycle audit of mutating events on a handover doc: `generated` / `archived` / `restored` / `edited` / `promoted`.

**Handover body history.** A row in `handover_body_history`. A pre-overwrite snapshot of a handover doc's body Markdown. Bounded at 20 rows per doc. → See [07 §14](./07-collaboration-system.md#14-v110--audit-timeline--diff-viewer).

**HNSW index.** The pgvector index type used for embeddings. Parameters `m=16, ef_construction=64`, cosine ops. See `src/bifrost/storage/models.py:182-188`.

---

## I

**`inherit_user_mcp`.** A flag on a `claude -p` call that makes the spawned CLI inherit the operator's authenticated MCP servers (Atlassian, Slack, GitHub). Set on research-agent calls; *not* set on extraction-pipeline calls (those run in a neutral cwd with MCP stripped to keep the prompt small).

**Intent.** The user's original ask, stored as `intent_text` on a Task row. May be augmented by `acceptance_criteria` parsed from the discussion or from the upstream Jira issue.

**Intent router.** The action-layer router (`src/bifrost/actions/intent_router.py`) that classifies a freshly created task — whether it's a research question, a coding task, a documentation update, etc. — and routes to the appropriate sub-agent.

---

## J

**Jira agent.** A research sub-agent (`src/bifrost/research/jira_agent.py`) wrapping the Atlassian MCP for Jira. Reads `jira_issues` cache first.

---

## K

**Knowledge index.** The compressed (≤5K tokens per repo) summary produced in [Phase H](#p) of the pipeline. Per-entity entries with high-level descriptions; meant for quick LLM ingestion in chat contexts.

**Knowledge tier.** See *[Entity tier](#e)*.

**KTLO.** Keep The Lights On — operational toil. Used in the "KTLO surface" section of a handover doc (oncall rotation, runbooks, alerts, SLOs).

---

## L

**Learning loop.** The continuous-learning cycle that compounds verify outcomes back into the knowledge graph. A verify pass nudges confidences up; a verify fail nudges them down. The audit trail lives in `confidence_deltas`; the admin surface is `/admin/learning`.

**Lifespan.** The FastAPI async lifespan (`src/bifrost/api/app.py:28-131`). Runs once at startup and once at shutdown. See architecture §2.1.

**Loop-back.** The pattern where a doubt that recurs across many collaboration sessions becomes a candidate update to the parent handover doc. The substrate (handover doc) gets enriched by the interaction (sessions). Implemented in `src/bifrost/collaboration/loopback.py`. → See [07 §18](./07-collaboration-system.md#18-end-to-end-loop-back-v13-w2--v14-w2).

---

## M

**Manifest.** The per-repo metadata file Bifrost reads for ownership and runtime settings: Backstage `catalog-info.yaml`, `CODEOWNERS`, or a YAML file mapped by the `generic_yaml` provider. Providers are listed in `manifest.providers` in `bifrost.yaml`. Parsed into `repo_ownership` (with the merged result in `raw_manifest`).

**Manifest hash.** Sha256 of `(sorted source URNs + extraction timestamps)` on a handover doc. Stale-detection signal: when the underlying sources change, the hash changes, and the handover is flagged stale.

**Matching.** The phase at the start of a collaboration session that finds the handover docs the session is grounded in. Hybrid TSVECTOR + pgvector lookup; `src/bifrost/collaboration/matching.py`.

**MCP server.** Model Context Protocol server. Subprocesses of the Claude CLI that expose external tools (Atlassian, Slack, GitHub). Bifrost does not run MCP servers itself — it spawns the CLI with `inherit_user_mcp=True` and the CLI inherits the operator's authenticated MCP servers.

**MCP tool.** A specific tool exposed by an MCP server (e.g. `mcp__atlassian__searchJiraIssuesUsingJql`). Called from inside the spawned Claude CLI's reasoning loop.

---

## N

**Neutral cwd.** A scratch directory used by the Claude CLI client when no specific working tree is appropriate (extraction calls run in a neutral cwd so MCP server inheritance is sandboxed). See `ClaudeLlmClient._ensure_neutral_cwd`.

---

## O

**Onboarding lens.** A synthesis variant of the handover prompt — generates a "Day 1 / Week 1 / First quarter" view from the same source data as the standard handover.

**Org catalog.** See *[Catalog](#c)*.

**Org repo.** A row in `org_repos`. The catalog rows from `gh api orgs/<org>/repos`, enriched best-effort with manifest metadata.

---

## P

**Parent path (handover).** The canonical parent slot a handover doc takes in the doc tree. The DAG (a feature can belong to multiple domains) is collapsed to a tree by picking one parent; cross-references to non-parent scopes are links, not parents.

**pgvector.** The Postgres extension providing the `Vector(N)` column type and ANN indexes (HNSW + IVFFlat). Bifrost uses `Vector(384)` and HNSW.

**Phase (extraction).** See *[Extraction phase](#e)*.

**Phase (task).** A unit inside a Task. Each task has an ordered list of phases; each phase runs the four-step loop. See `task_phases`.
*Don't confuse with.* [Extraction phase](#e) — that is the A-I letter sequence; this is an integer ordinal within a task.

**Plan agent.** The action-layer agent (`src/bifrost/actions/plan_agent.py`) that drives the *Plan* step of a phase. Drafts and revises the plan artifact, then parses it into `task_plans` rows.

**Plan key.** The stable identifier of a `task_plans` row within a phase, e.g. `P1-T3` (Phase 1, Task 3).

**Primary repo.** A repo with `is_primary=true` (or the manifest sets `is_primary`). The canonical answer for chat queries when a domain has multiple repos.

**Prompt safety.** Helpers in `src/bifrost/actions/_prompt_safety.py` that wrap untrusted text in a clearly-delimited boundary so the LLM treats it as data rather than instruction. Used everywhere a user-supplied or external-source string ends up in a prompt.

---

## Q

**Quick action.** The action-layer's fast-path for trivial requests that do not need full Discuss / Plan / Execute / Verify. See `src/bifrost/actions/quick.py`. Exits the loop early when the intent router classifies a task as quick.

---

## R

**Region.** A geographic region (e.g. us, eu, apac). Maps to repos via `org_repos.region`. Most expensive handover-doc scope.

**Repo.** A registered source repository. Row in `repos`. URN-keyed. Distinct from an *[org repo](#o)* (a row in `org_repos`).

**Repo agent.** A research sub-agent (`src/bifrost/research/repo_agent.py`) that searches a single cloned repo for relevant code snippets, given a question and a scope.

**Repo freshness.** The `repo_freshness` row tracking a repo's extraction state — last-indexed commit, files analysed, current `freshness_state`, mid-pipeline checkpoint cursors.

**Requirement.** The `requirement_text` on a collaboration session — the original ask the requester brought in.

**Research bundle.** See *[Bundle](#b)*.

**Researcher.** Generic name for a research sub-agent. Each researcher is a `claude -p` call with a sub-task-specific system prompt and a JSON schema.

**Retracted (assumption).** State `retracted` on `task_assumptions`. Set when a downstream artifact contradicts the assumption. `retracted_by_artifact_id` records which artifact retracted it.

**Router (chat).** The Haiku-grade ranker (`src/bifrost/research/router.py:rank_repos`) that picks the top-K repos relevant to a question before fan-out.
*Don't confuse with.* [Router (intent)](#r).

**Router (intent).** See *[Intent router](#i)*.

---

## S

**Scope (chat).** A user's optional restriction on which repos / domains a chat question should consider. Lives on the `Conversation` row as `scope_repo_ids` + `scope_domain_id`.

**Scope kind.** The kind of scope a handover doc applies to. Enum: `feature` / `repo` / `domain` / `region` / `team` / `session`.

**Scope value.** The slug for a scope (e.g. `delivery` for a domain, `eu` for a region, a session UUID for a session-scoped handover).

**Section (handover).** One of the canonical sections of a handover doc: executive summary, ownership map, architecture, critical flows, dependencies, KTLO surface, domain entities, active work, documentation index, collaboration playbook, glossary. Section presence is adaptive to scope kind.

**Secret scanner.** `src/bifrost/extraction/secrets.py`. Runs over file content before extraction. Redacts detected secrets in-place; emits `RedactionEvent`s for audit.

**Service tier.** A column on `repo_ownership` (and `org_repos`) classifying a service's criticality (`tier-0` / `tier-1` / etc.). Sourced from the repo manifest.

**Session (chat).** A cookie-based identity used to attribute conversations. Stored as `Conversation.session_id`.

**Session (collaboration).** Short for `CollaborationSession`. The stateful collaboration interaction.

**Session pool config.** Track-B-only concept (S8 / S9). Caps the concurrent Claude sessions when running on Managed Agents. Not relevant for Track A.

**Skim search.** A chat fallback when the question can't be answered from registered repos alone. Searches `org_repos` and surfaces candidate repos to register.

**Slack agent.** A research sub-agent (`src/bifrost/research/slack_agent.py`) wrapping the Slack MCP. Looks up channel discussions for a repo (channel resolved via `repo_ownership.slack_channel`).

**SSE.** Server-Sent Events. The streaming push channel from backend to UI for extraction progress, chat tokens, and collaboration session updates. See `src/bifrost/api/sse.py`.

**Stale.** Freshness state meaning "the underlying source has changed since we last refreshed." Multiple shades: `stale` / `very_stale` / `stale_due_to_children` (handover docs only).

**Source basis.** A JSONB array of evidence URNs supporting an entity or relationship. Carried on every entity / relationship / assumption.

**`source_type`.** Provenance enum on `task_assumptions`. One of `user_stated` / `code_verified` / `manifest` / `gradle` / `package_json` / `metadata_match` / `confluence` / `jira` / `github_issue` / `slack` / `fuzzy_match` / `llm_guess` / `research`.

**Synthesizer (chat).** The final agent in the chat coordinator. Takes the per-source reports and produces a streamed answer with citations.
*Don't confuse with.* [Synthesizer (collaboration)](#s).

**Synthesizer (collaboration).** The agent (`src/bifrost/collaboration/synthesizer.py`) that runs once when a session reaches `resolved` and produces the final brief Markdown.

---

## T

**Team.** The owning team named in a repo's manifest. Stored in the `squad` column of `repo_ownership` and `org_repos`. A handover doc can be scoped to a team, and `POST /domains/{id}/discover-repos` finds repos by team.

**Task.** Two senses (one of the most overloaded terms in the codebase):
1. **Action layer.** A user request to Claude to perform a coding change end-to-end. Row in `tasks`. State machine in `task_lifecycle.py`.
2. **Extraction.** A row in `extraction_tasks` (pre-rename: `tasks`). The extraction-job lifecycle entity.

The codebase is unambiguous — `extraction_tasks` and `tasks` are distinct tables. The handbook always qualifies "task" as either "extraction task" or "user task" when context could mislead.

**Task event.** A row in `task_events`. Append-only audit log per Task. Actor: `user` / `agent` / `system`. Action is free-text; payload is JSONB.

**Tier.** Three senses (overloaded — pin down the context):
1. **Entity tier (knowledge graph).** 0-3 small-int classifying knowledge breadth. → See [Entity tier](#e).
2. **Assumption tier (action layer).** 1-5 provenance grade on `task_assumptions`. Different scale.
3. **Service tier.** `tier-0` / `tier-1` etc., sourced from the repo manifest. → See [Service tier](#s).

**Track A.** The default implementation. PostgreSQL + `claude -p` subprocess + in-process SSE bus. Today's only working track.

**Track B.** The future implementation. Anthropic Managed Agents + Memory Stores + MCP. Gated on Research Preview access. Drop-in by design.

**Triage.** Phase A of the extraction pipeline. Haiku-grade file classification — picks which files are worth full extraction and which are skippable (vendored code, generated stubs, etc.). `src/bifrost/extraction/triage.py`. → See [06 §2](./06-extraction-pipeline.md#2-phase-a--triage-haiku).

**TSVECTOR.** Postgres full-text search vector. Used on `handover_docs.search_tsv` (Postgres-generated, GIN-indexed, used with `@@` and `ts_rank` for keyword search).

---

## U

**URN.** Bifrost's canonical identifier scheme. Form `bifrost:org:{orgId}:{kind}:{kind-specific-id}`. Every Source, SourceObject, Repo, Domain, Entity, Relationship, etc. has one. Used as the unique key on most tables.

---

## V

**Verify.** The fourth step of an action-layer phase. Runs the checklist of `task_verify_items` against the result of execution and either passes the phase to the next or transitions the task to `blocked`.

**Verify cmd.** The optional shell command on a `task_plans` row used to verify the step's effect (e.g. `npm test -- --filter=auth`).

**Very stale.** A freshness state used on handover docs when even the "stale" threshold has been long exceeded. Triggers a more aggressive re-generation policy.

---

## W

**Wave (plan).** A parallel-execution grouping on `task_plans`. Plans in the same wave can run concurrently; later waves wait for earlier waves.
*Don't confuse with.* [Wave (handover build)](#w).

**Wave (handover build).** A parallel-execution level in the multi-repo handover build. Children run in one wave, parent in the next. → See [07 §7](./07-collaboration-system.md#7-v13--tree-decomposition--loop-back--haiku-matcher--multi-scope-stale).

**Worktree.** A per-task git worktree created at the `Executing` phase and torn down on `cancelled`. Path stored on the `tasks.worktree_path` column.

---

## What's not in this glossary

- Anything that is a pure UI concept (button labels, page sections) — those are covered by the operational chapters.
- API request/response field names — read `src/bifrost/api/schemas/` directly when constructing requests.
- Spec-only terms that do not appear in code (e.g. "Specialist" from F1 §4 — Bifrost code uses "agent" exclusively).
- Anthropic-platform terms (Sonnet, Haiku, Memory Store, Managed Agent) — Anthropic's documentation is the authoritative source.

---

## Where to read next

- For the *what* of every persisted row using these terms, [03 — Data model](./03-data-model.md).
- For the *where* of every code path using these terms, [02 — Architecture](./02-architecture.md).
- For the *why* of the system, [01 — Vision and overview](./01-vision-and-overview.md).
- For the extraction pipeline that mints most of these entities, [06 — Extraction pipeline](./06-extraction-pipeline.md).
- For the collaboration substrate that shapes Brief / Doubt / Handover meanings, [07 — Collaboration system](./07-collaboration-system.md).
