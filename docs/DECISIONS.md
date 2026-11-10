# Bifrost decisions

> **Read this when** you want to know why a part of Bifrost looks the way it does, what failed along the way, or what comes next.
> **Skip if** you want how it works today. The [handbook](handbook/README.md) covers that.

ADR ids such as D8, D9 and D11 refer to the [tech-stack ADR](archive/specs/tech-stack-adr.md), which was committed with the first code commit (97afe50) and no longer matches the code in several rows. Experiment decisions D1 to D4 are in [roadmap section 13](roadmap.md#13-decisions-taken-2026-11-05).

---

## Decisions

### 1. Every model call is a `claude -p` subprocess, not the SDK

**Context.** The original plan called the Messages API with an SDK key (ADR D8). The shipped client replaces that with a subprocess per call, using the operator's Claude login.
**Decision.** `src/bifrost/extraction/cli_client.py` spawns `claude -p` with `--no-session-persistence`. Structured calls (`call_with_json_schema`) also pass `--json-schema`; the streaming text path (`stream_text`) does not. Cost comes from the CLI's `total_cost_usd` ([handbook 02 section 3.1](handbook/02-architecture.md#31-the-claude--p-subprocess-pattern)).
**Alternative rejected.** The `anthropic` SDK with an API key. The package is still in `pyproject.toml`, and only `extraction_probe/agent.py` imports it.
**Why.** There is no API key to provision, store or rotate, and usage rides on the operator's Claude login ([handbook 02 section 3.1](handbook/02-architecture.md#31-the-claude--p-subprocess-pattern)).
**Costs.** Every call is a new process. Calls from `stream_text` and the default `call_with_json_schema` pass an empty MCP config with `--strict-mcp-config` and no setting sources, so a spawn loads about 200 tokens of baseline instead of 50K (handbook 02, same section). The per-source research agents and the external indexer pass `inherit_user_mcp=True`, which skips `--strict-mcp-config` and loads the operator's user MCP servers. The server needs a logged-in Claude Code on the machine that runs it, so every user of that server spends from one login. The benchmark runner does not record the CLI version, so an upgrade between two runs does not show in the results.

### 2. One async process, no Celery or Redis

**Context.** Extraction, chat and the janitors all need background work.
**Decision.** They run as `asyncio` tasks inside the FastAPI process, started from the lifespan in `src/bifrost/api/app.py`.
**Alternative rejected.** Celery with Redis. ADR D9 gives the reason "Heavier job model than V1 needs". The ADR's own pick, an `event_log` table with `LISTEN/NOTIFY`, was never built; the bus is in memory.
**Tradeoff.** The SSE bus in `src/bifrost/api/sse.py` is a dict of queues, so a second worker process would not see the first one's events. Handbook 02 section 10, item 1, says multi-worker scaling needs Redis or a sidecar.
**Why.** [Handbook 02 section 2.2](handbook/02-architecture.md#22-why-single-process) gives three reasons: operator simplicity, no cross-process state, and Claude CLI wall time dominating latency.

### 3. Postgres with pgvector HNSW, and local MiniLM vectors at 384 dimensions

**Decision.** One Postgres 16 database holds the graph and the vectors. `storage/models.py` declares `Vector(384)` columns, `alembic/versions/001_baseline.py` builds HNSW indexes on them, and `research/embeddings.py` computes them with `all-MiniLM-L6-v2` on CPU.
**Why.** ADR D11 picks pgvector because it keeps vectors in the same database as the graph. It rejects IVFFlat ("degrades above ~10K vectors"), Pinecone ("data residency concern for RESTRICTED repos") and Weaviate ("adds another Docker service"). The embedding model runs on CPU with no API key (comment in `pyproject.toml`).
**Not evaluated.** The ADR does not weigh a graph database such as Neo4j against Postgres tables. At about 0.12 edges per entity (decision 4) the walk is a bounded BFS over one table (`research/graph_retrieval.py`), and nobody compared it with a graph store.
**Tradeoff.** MiniLM truncates at 128 tokens and is a general text model, not a code model ([roadmap section 6.1](roadmap.md#61-current-state)). The ADR's 1536 dimensions are stale, and two tests that still use 1536-dimension vectors are xfail-marked.

### 4. Extraction is LLM-only, with no parser

**Decision.** `extraction/pipeline.py` runs LLM phases A, D, E, F, G, H, I and J. B and C are the clone and file listing in the git adapter. There is no tree-sitter, AST or SCIP layer, and the graph has about 0.12 edges per entity (docstring in `research/graph_retrieval.py`).
Phases A and D to I are the seven the handbook describes. Phase J writes a short repo summary into `repos.description`, and a failure there is recorded and not fatal.
**Mechanism.** An LLM pass needs no per-language parser, and the graph comes out sparse.
**Tradeoff.** The [roadmap verdict](roadmap.md#1-verdict) says leading systems extract structure with parsers and use the model to explain it. Card P2.3 plans a tree-sitter symbol layer. I do not defend the sparse graph as a win: it is a known gap with a planned experiment.

### 5. Slack, GitHub and Confluence agents are opt-in

**Decision.** The three per-source agents are off unless chat settings turn them on. The `research_include_*` field descriptions in `api/schemas/chat_settings.py` give a different second reason for each:

- Slack: "Off by default because Slack has ~0-5% citation rate and adds 10-40s per query."
- GitHub: "Off by default because GitHub has ~0-5% citation rate and adds 10-15 agent calls per query."
- Confluence: off by default because another agent already searches Confluence and per-repo coverage overlaps with it (paraphrased from the same field description).

**Exception.** When a question carries a Jira ticket chain, GitHub is enabled for high-relevance repos hosted on github.com (`research/coordinator.py`, in the Jira-chain handling of `run_research`).
**Tradeoff.** Answers miss recent chat and PR context unless you opt in. [Roadmap section 6.3](roadmap.md#63-pushback-on-the-per-source-fan-out) argues against adding more per-source agents and tests one hybrid index instead (P3.1).

### 6. Slack escalation has no model in the path

**Decision.** `collaboration/slack_escalation.py` posts the draft verbatim to one configured channel through the Slack Web API. Each call is tried twice, then a typed error is raised and the route returns 502.
**Why.** The module docstring: "the body goes verbatim to the one configured channel, so nothing inside the body can redirect the post."
**Replaced.** The first version let an agent post through the operator's Slack MCP. It was best-effort and never blocked the resolve (`collaboration/slack_escalation.py` and `api/routes/collaboration.py` before commit 97e27d0, 2026-10-21). That commit swapped it for the Web API post. The docstring rules the old path out ("no LLM, no subprocess, no MCP").
**Tradeoff.** It needs a bot token with `chat:write` and one fixed channel. Evidence: `tests/test_slack_escalation_deterministic.py::test_ok_false_is_retried_once_then_raises_typed_error`.

### 7. Extraction and research run under `dontAsk` with an explicit tool list

**Decision.** `_permission_args` in `extraction/cli_client.py` passes `--permission-mode dontAsk` and `--allowedTools`, so anything not listed is denied.
**Why.** The docstring of `tests/test_cli_client_permissions.py`: `bypassPermissions` "approved every tool of every inherited MCP server, so attacker-influenced content read by an agent could drive any operator tool."
**Tradeoff.** Every tool an agent needs must be named. The task executor, verifier and debugger still use `bypassPermissions`; [SECURITY.md](../SECURITY.md#task-execution) documents that risk. Evidence: `test_inherit_mode_still_loads_user_mcp_but_never_bypasses`.

### 8. Startup fails fast on configuration

**Decision.** A `BIFROST_` name or top-level `bifrost.yaml` key that nothing reads stops startup and suggests the closest name. Production mode refuses the dev database URL. The dev defaults must agree across compose, `alembic.ini`, `.env.example` and CI.
**Why.** `tests/test_unknown_settings.py`: "pydantic-settings ignores unknown keys, so a typo (or a name from an older release) silently leaves a gate off."
**Tradeoff.** An old `.env` with a removed name blocks startup until you edit it.
**Evidence.** `test_unknown_settings::test_error_suggests_the_closest_name`, `tests/test_production_mode.py::TestProductionModeOn::test_dev_database_url_errors`, `test_dev_defaults_agree::test_env_example_matches_compose`.

### 9. A regex scorer, and measure before tuning

**Decision.** `benchmarks/scorer.py` scores answers with regex and substring matching over seven components: recall 0.25, specificity 0.20, precision 0.15, evidence consistency 0.15, coverage 0.10, format adherence 0.10 and tier integrity 0.05.
**Why these weights.** The comment above `DEFAULT_WEIGHTS` says recall and specificity are "the operator's main complaints" when an answer is bad. Evidence consistency catches "headline=confirmed but body says 'not routed'", and its weight reflects over-claiming under thin evidence in multi-turn runs.
**Mechanism and tradeoff of a regex scorer.** It makes no model call, so scoring costs nothing per run, and it cannot check that a cited anchor exists. [Roadmap D1](roadmap.md#d1--does-phase-1-wait-for-the-phase-0-judge) keeps it until a calibrated judge exists (card E0.2) and puts measurement before tuning.
**Tradeoff.** Gains under about 0.01 are unreadable ([roadmap D1](roadmap.md#d1--does-phase-1-wait-for-the-phase-0-judge)); failure 6 measures a 0.007 spread. Specificity counts `file:line` anchors without checking that they exist ([roadmap section 6.1](roadmap.md#61-current-state)). `tests/test_prompt_benchmark_isolation.py::test_prompt_has_no_acme_terms` keeps benchmark answers out of the prompts.

### 10. A second research pipeline still ships

**Context.** `agents/` and `orchestration/` date from the first code commit (97afe50, 2026-04-24) and sit behind `POST /api/v1/ask` (`api/routes/ask.py`). Chat uses `research/`. Only tests call the older route.
**Decision.** Neither is removed yet. `extraction_probe/agent.py` is likewise a standalone experiment, and it is why `anthropic` is still a dependency.
**Tradeoff.** The repo carries two answer paths, so a reader has to be told which one is real (`research/`). Deleting `agents/`, `orchestration/` and `extraction_probe/` would drop the `anthropic` dependency, and decision 1 would then be true without a footnote.

---

## What did not work

1. **The test schema was built with `Base.metadata.create_all`.** That only knows ORM tables, so it wiped the 17 tables that exist only in migrations and broke every later test that used them. The schema is now built once per session with alembic (`tests/conftest.py`).
2. **alembic's `fileConfig` disabled every existing logger.** `caplog` tests then captured nothing. The fixture re-enables the loggers after the migration runs (`tests/conftest.py`).
3. **EventSource reconnects lost events, and users re-sent their questions.** A tab switch or flaky network closes the stream, and the browser reopens it with an empty queue. Each task now keeps a replay buffer of 200 events with a 5-minute TTL, and terminal events survive the TTL (docstring in `api/sse.py`).
4. **Three fire-and-forget auth probes ran at startup.** The lifespan dispatched a `claude auth status` probe with a 5-second cap, a `git ls-remote` probe and a `gh auth status` probe. Commit fbe27e6 (2026-06-03) removed all three, and its subject claims startup dropped from about 10s to under 1s. The commit body calls them diagnostic only and says they forked subprocesses during startup. Auth is now checked when an extraction runs (`extraction/worker.py`) and through `GET /health/claude-cli`.
5. **A Dependabot bump (2026-05-25) moved `@eslint/js` to 10 while `eslint` stayed on 9.** A fresh `npm ci` then exited 1 with ERESOLVE (peerOptional `eslint` ^10 from `@eslint/js` 10.0.1, commit 38d8154). `ui/package.json` now keeps `@eslint/js` on `^9.39.4`.
6. **The benchmark cannot see small gains.** Three identical baseline runs scored 0.734, 0.741 and 0.738, a spread of 0.007, and a later experiment reported a gain of +0.006. Measurement (card E0.1, the noise floor) now comes before tuning ([roadmap sections 1 and 2](roadmap.md#1-verdict)).
7. **Extraction routes submitted the task before committing it.** The worker runs in a separate session and could run first, which failed with "Task not found". The routes now commit before they submit (`tests/test_extract_commit_order.py::test_task_is_committed_before_submit`).

Two failures are still open and have plans but no fix. The adversarial verifier times out on about 30% of benchmark turns (card P1.2). The planner dispatched no agents on q3, q4, q5 and q12 in the baseline runs (card P1.1). Both are measured in [roadmap section 6.2](roadmap.md#62-the-question-pipeline-and-its-gaps).

---

## What I would change next

- Follow the order in [roadmap section 2](roadmap.md#2-decided-plan), where P1.1, the planner floor, ships first as a bug fix. [Section 8.3](roadmap.md#83-top-ten) ranks the same items with measurement first: E0.1 noise floor, E0.2 calibrated judge and E0.3 citation existence check, then P1.1. The tree-sitter symbol layer (P2.3) is not in the top ten; typed cross-service edges (P2.2) come ahead of it ([section 8.2](roadmap.md#82-why-the-order-looks-like-this)).
- Before more than one user, work through the [standing security gate](roadmap.md#812-standing-security-gate-before-multi-user-use), which includes the executor's `bypassPermissions`.
- Fix the 27 `npx tsc -b` errors, then point the CI typecheck at `tsc -b`. Today it runs `tsc --noEmit` against a `tsconfig.json` with `"files": []` and checks nothing.
- Split `src/bifrost/research/coordinator.py`, which is 6301 lines. [Roadmap section 8.2](roadmap.md#82-why-the-order-looks-like-this) notes that AI help shrinks this kind of refactor the least.
- Replace the process-local SSE bus for a multi-worker setup (handbook 02 section 10, item 1).
- Delete or merge the second research pipeline and `extraction_probe/` (decision 10), then drop `anthropic` from `pyproject.toml`.
- Record the Claude CLI version with every benchmark run. The runner does not do this today.
