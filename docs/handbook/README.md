# Bifrost Handbook

> **TL;DR.** This handbook is the single artifact you can sit down with and
> read top-to-bottom to understand Bifrost end-to-end without grepping the
> codebase. It is opinionated about what the system *is today* (not what the
> specs aspire to). It is split into a foundational half (vision, architecture,
> data model, glossary — chapters 01-03 and 09) and an operational half
> (chapters 04-08, written separately). Every load-bearing claim cites a
> source file.

> **Read this when** you are onboarding to Bifrost, returning after time away,
> or about to make a non-trivial change anywhere in the system. **Skip if**
> you already know the topology and just need a specific table or endpoint
> reference — jump straight to chapter 03 or the [glossary](./09-glossary.md).

---

## What this is

A read-from-the-top handbook for Bifrost. It is the single
artifact you can sit down with and walk through to understand the entire
system end-to-end without grepping the codebase. It is opinionated about
what "the system" is *today*, not what the specs aspire to.

The handbook is split into two halves. **This (foundational) half** covers
vision, architecture, the data model, and shared vocabulary. The **operational
half** (chapters 04-08, written separately) covers individual product
surfaces (chat, tasks, collaboration), runbooks, ops, and gotchas.

If a fact is not in the code, it is not in this handbook. Wherever a claim
required reading more than the file in front of the reader, the line is annotated.

---

## 5-minute tour

If you only have time to skim, these are the five questions a new reader
should be able to answer after this handbook. Each links straight to its
chapter.

| Question | Answered in |
|---|---|
| What is Bifrost and why does it exist? | [01 §1-3](./01-vision-and-overview.md) |
| What does the runtime look like? Where does extraction happen? | [02 §1-3](./02-architecture.md) |
| What is the data model — what's actually persisted? | [03](./03-data-model.md) |
| How does collaboration (team-to-team brokerage) work? | [01 §4.5](./01-vision-and-overview.md#45-collaboration-collaboration-collaboration) and [02 §6.4](./02-architecture.md#64-collaboration-session) |
| What's the canonical vocabulary used everywhere? | [09 — Glossary](./09-glossary.md) |

If you can answer all five after one pass, the handbook has done its job.

---

## Read in order

1. **[01 — Vision and overview](./01-vision-and-overview.md)**
   What Bifrost is, why it exists, the seven product surfaces today, the
   user journey from "a question about a system" through "a brief that can be
   taken to another team."
   *Skip if* you already know the elevator pitch and just want runtime details.

2. **[02 — Architecture](./02-architecture.md)**
   System topology, process model, the `claude -p` subprocess pattern, MCP
   sidecars, the in-memory SSE bus, the major code packages under
   `src/bifrost/`, and the four data flows that matter
   (extraction, chat, handover generation, collaboration session).
   *Skip if* you already know the topology and only need data-model specifics.

3. **[03 — Data model](./03-data-model.md)**
   Every persisted table grouped by purpose. Inventoried from
   `src/bifrost/storage/models.py` and `alembic/versions/`. Includes
   four ER-diagram blocks (one per group) and a per-table "what / when /
   who reads" paragraph.
   *Skip if* you are not writing migrations or queries today.

4. **04-08** — operational chapters (separately authored).
   *Skip if* you are doing architectural work; come back when you need to
   wire a new endpoint, add a UI page, or debug an extraction.

5. **[09 — Glossary](./09-glossary.md)**
   ~50 domain terms, alphabetised. The vocabulary the codebase, schema,
   and other handbook chapters all assume.
   *Skip on first read* — bookmark for the moment a term feels overloaded.

The ordering is deliberate: vision sets the why, architecture sets the
what-runs-where, data model sets the what-is-stored. Once those three are
in your head, the operational chapters and the glossary read as look-up
references rather than narrative.

> **Remember:** read 01-03 in order. The glossary is a reference, not a
> chapter — jump to it on demand.

---

## Quick reference

The questions an owner asks first, mapped to where the handbook answers
them.

### How does X work?

| Question | Chapter |
|---|---|
| How does extraction turn a `git clone` into entities? | [02 §6.1](./02-architecture.md#61-repo-extraction-7-phases) |
| How does a chat question fan out to per-source agents? | [02 §6.2](./02-architecture.md#62-chat-query-coordinator-fan-out--synthesis) |
| How does handover-doc generation produce `index.md` on disk? | [02 §6.3](./02-architecture.md#63-handover-generation) |
| How does a collaboration session move from `open` to `briefed`? | [02 §6.4](./02-architecture.md#64-collaboration-session) |
| How does the action layer (Discuss / Plan / Execute / Verify) work? | covered by the operational chapters |
| How does the system spawn LLM calls without an API key? | [02 §3.1](./02-architecture.md#31-the-claude--p-subprocess-pattern) |
| What's the difference between `extraction_tasks` and `tasks`? | [03 §5](./03-data-model.md#5-extraction--lifecycle-tables) |

### Where does Y live?

| Concern | Path |
|---|---|
| FastAPI app entry | `src/bifrost/api/app.py` |
| All HTTP routers | `src/bifrost/api/routes/__init__.py` |
| All ORM models | `src/bifrost/storage/models.py` |
| Schema migrations | `alembic/versions/` |
| Extraction pipeline (7 phases) | `src/bifrost/extraction/pipeline.py` |
| Claude CLI subprocess client | `src/bifrost/extraction/cli_client.py` |
| Chat coordinator (multi-repo fan-out) | `src/bifrost/research/coordinator.py` |
| Handover-doc generator | `src/bifrost/collaboration/handover_generator.py` |
| Collab clarifier loop | `src/bifrost/collaboration/clarifier.py` |
| Collab synthesizer (closes the session) | `src/bifrost/collaboration/synthesizer.py` |
| Task-state machine (action layer) | `src/bifrost/actions/task_lifecycle.py` |
| In-memory SSE event bus | `src/bifrost/api/sse.py` |
| Frontend route table | `ui/src/App.tsx` |
| UI pages | `ui/src/pages/` |
| dev.sh / health_check.sh / restart.sh | `scripts/` |

### What runs when Z?

- **A new HTTP request hits the API.**
  `RequestLoggingMiddleware` wraps every request with structlog timings,
  and `IdentityMiddleware` requires a principal on everything except the
  public health and auth routes (`src/bifrost/api/app.py`,
  `src/bifrost/api/auth/middleware.py`).

- **The API process starts.**
  The async `lifespan` fires a fire-and-forget Claude CLI auth probe,
  starts the `ExtractionWorker`, schedules the hourly task auto-archive
  sweep, and starts the SSE replay-buffer janitor (every 2 minutes) —
  see `src/bifrost/api/app.py:28-131`. You'll see something like this in
  `nohup.out`:

  ```
  bifrost.startup settings_loaded=True
  bifrost.cli_auth.probe_dispatched timeout_s=5
  bifrost.extraction_worker.started
  bifrost.lifespan.scheduled task=auto_archive interval_h=1
  bifrost.lifespan.scheduled task=sse_janitor interval_s=120
  ```

- **An entity needs to be embedded.**
  Local sentence-transformers `all-MiniLM-L6-v2` (384-dim), no network
  egress (`src/bifrost/storage/models.py:134`).
  → see [09 §URN](./09-glossary.md#u) for the canonical entity ID format.

- **A handover doc is regenerated.**
  Single Sonnet call with a sectioned JSON schema (small scopes), or a
  wave-based child→parent build (multi-repo scopes ≥ 5 repos). The
  threshold is documented at
  `src/bifrost/collaboration/handover_generator.py:80-90`.

- **A user asks a chat question.**
  The query route delegates to `query_worker.run_query` which constructs
  a `ResearchCoordinator` (`src/bifrost/research/coordinator.py`).
  Coordinator fans out per-source agents in parallel (Jira, Confluence,
  GitHub, Slack, per-repo) and merges into a synthesised answer streamed
  over SSE. → see [02 §6.2](./02-architecture.md#62-chat-query-coordinator-fan-out--synthesis).

> **Remember:** the runtime is a single async FastAPI process plus
> Postgres. Anything that looks like a "background worker" is really an
> `asyncio` task inside that one process.

---

## Read state markers

Throughout the handbook, claims are tagged with one of three markers when
their source is not obvious from the surrounding text. The convention exists
because this codebase is large enough that "read it somewhere" is not a
rigorous citation.

- `[verified]` — a direct quote or close paraphrase of code currently on
  the branch. Always accompanied by a `path:line` citation.

- `[inferred]` — a claim derived by joining two or more facts that are
  themselves verified, or by reading a top-of-file docstring that
  describes the file's intent. Treat these as 95%-true but worth a glance
  before quoting.

- `[needs verification]` — a claim that seemed obvious from context but was
  not pinned to a specific file/line. These are the rough
  edges — if you're going to cite the handbook for something high-stakes,
  audit anything tagged with this marker first.

Untagged statements are either definitions, reasoned framing, or
small-grain code descriptions that the surrounding citation already
covers.

> **Remember:** treat the markers like SLOs on the doc itself. `verified`
> is "ground truth"; `needs verification` is "double-check before
> quoting in a design review."

---

## What this handbook does not cover

- **Specs.** This handbook describes what is *built*. The archived specs
  (`docs/archive/specs/`) are pre-build plans kept for reference and may not match
  the code. The handbook and [docs/roadmap.md](../roadmap.md) say what is true now
  and what is planned.

- **Roadmap.** The research survey and the ranked experiment plan are in
  [docs/roadmap.md](../roadmap.md). It records planned experiments, not
  shipped behaviour.

- **Working plans.** Per-project plans change daily. The handbook is
  intentionally stable and is updated only on a major milestone.

- **Operations runbooks.** Health endpoint, restart procedure, log
  locations, the `nohup.out` recovery flow — operational chapters 04-08
  cover those.
