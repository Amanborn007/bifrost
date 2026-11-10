# 10 — Operations

> Run it. Restart it. When it breaks, fix it. The single page you should bookmark when something's on fire.

> **TL;DR.** Local dev runs Postgres in Docker, the API on `:8000` via `scripts/restart.sh`, and Vite on `:5173`. The restart script double-forks so `claude -p` subprocesses survive. Auth comes from `claude auth login` — never API keys. Roles (`member`, `admin`), `bft_` API tokens and a per-cookie rate limit protect any shared environment. When extraction or handover generation breaks, the troubleshooting cookbook below has symptom → diagnosis → remediation triplets for every common failure.

> **Read this when** you're starting up after a fresh clone, recovering from a broken extraction, generating an admin token, or diagnosing a stuck handover.
> **Skip if** you only consume the running system through the UI — the previous chapters cover what each surface does.

---

## 1. Running locally

### 1.1 First-time setup

```bash
# Clone + change into repo
cd bifrost

# Python deps via uv (pyproject.toml)
uv sync

# UI deps
cd ui && npm install && cd ..

# Postgres + pgvector
docker-compose up -d            # starts the local DB on :5432

# Apply migrations
uv run alembic upgrade head

# Authenticate the Claude CLI (OAuth login, not an API key)
claude auth login

# Copy the env template
cp .env.example .env             # edit as needed
```

**Expected output of `uv run alembic upgrade head`** (truncated):
```
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 001, baseline
```

### 1.2 The restart script

`scripts/restart.sh` is the canonical way to start (or restart) the API. It double-forks to detach from the tty so the long-running `claude -p` subprocesses do not inherit a terminal handle.

```bash
scripts/restart.sh                # API only on :8000
scripts/restart.sh --tail         # API + tail /tmp/bifrost_server.log
scripts/restart.sh --ui           # API + Vite dev server on :5173
scripts/restart.sh --ui --tail    # both, then tail
```

**Expected output (typical):**
```
[restart.sh] killing :8000 (was pid 4521)
[restart.sh] starting uvicorn detached
[restart.sh] up after 2s
[restart.sh] /api/v1/health/claude-cli → {"authenticated": true, "model": "claude-sonnet-4-6"}
[restart.sh] log smoke: worker_started=1, dry_run=0
```

What it does (`scripts/restart.sh:25-87`):
1. Kills anything listening on `:8000` (and `:5173` with `--ui`).
2. Unsets the stale `BIFROST_EXTRACTION_DRY_RUN` so the `.env` value wins.
3. Double-fork via Python so `uv run uvicorn …` runs detached.
4. Redirects stdout/stderr to `/tmp/bifrost_server.log`.
5. Polls `:8000` for up to 30s and prints `up after Xs`.
6. Hits `/api/v1/health/claude-cli` and dumps the response.
7. Greps the log for `dry_run` / `worker_started` lines as a smoke test.

### 1.3 Manual start (if you want stdout)

```bash
uv run uvicorn bifrost.api.app:create_app --factory --port 8000
```

Useful when you want to see logs in the terminal directly (the script logs to file).

### 1.4 UI dev server

```bash
cd ui
npm run dev           # → http://localhost:5173
```

The Vite proxy forwards `/api/v1/*` to `:8000` (configured in `ui/vite.config.ts`).

### 1.5 CLI

```bash
uv run bifrost --help
```

**Expected output:**
```
Usage: bifrost [OPTIONS] COMMAND [ARGS]...

  Bifrost CLI — knowledge graph operations.

Commands:
  extract        Trigger an extraction for a registered repo.
  handover       Generate / list / regenerate handover docs.
  catalog        Sync the org repo catalog.
  task           Manage action-layer tasks.
  ...
```

The CLI entry point is `bifrost.cli.app:app` (Typer). See `pyproject.toml:43-44`.

### If you remember nothing else
- `scripts/restart.sh --tail` is the one command you bookmark.
- `claude auth login` once per machine; Bifrost never carries an API key.

---

## 2. Environment variables

All read by `BifrostSettings` (`src/bifrost/cli/config.py`). Pydantic-settings prefix: `BIFROST_`.

### 2.1 Required

| Var | Default | Notes |
|---|---|---|
| `BIFROST_DATABASE_URL` | `postgresql+asyncpg://bifrost:bifrost_dev@localhost:5432/bifrost` | Async URL; production should rotate from Vault / AWS Secrets Manager. |

### 2.2 Models (Anthropic)

| Var | Default | Used by |
|---|---|---|
| `BIFROST_TRIAGE_MODEL` | `claude-haiku-4-5-20251001` | Phase A, intent router, scope matcher, planner classifier, quick fix |
| `BIFROST_EXTRACTION_MODEL` | `claude-sonnet-4-6` | Phases D-I |
| `BIFROST_AGENT_MODEL` | `claude-sonnet-4-6` | All reasoning agents (research, action, collab) |
| `BIFROST_CLAUDE_CLI_PATH` | `claude` | Path to the Claude Code CLI binary |
| `BIFROST_CLAUDE_CLI_TIMEOUT_SECONDS` | `600` | Per-call wall-clock cap |

> **No API keys.** Auth comes from `claude auth login`. If you find yourself reaching for `ANTHROPIC_API_KEY`, you are on the wrong path — Bifrost runs subprocesses against the logged-in CLI.

### 2.3 Extraction worker

| Var | Default | Notes |
|---|---|---|
| `BIFROST_MAX_CONCURRENT_EXTRACTIONS` | `1` | Per-host cap. Each extraction can spawn ~5 concurrent `claude -p` subprocesses (Phase D). |
| `BIFROST_CHECKPOINT_DIR` | `/tmp/bifrost-checkpoints` | Per-task resume state. |
| `BIFROST_CLONE_DIR` | `/tmp/bifrost-clones` | Where `git clone` lands. |
| `BIFROST_EXTRACTION_DRY_RUN` | `false` | When `true`, runs `_mock_extraction()` instead of real LLM calls. The restart script unsets this on every start. |

### 2.4 Roles and API tokens

Identity, roles and tokens live in `bifrost.yaml`, not env vars.

| Setting | Notes |
|---|---|
| `identity.roles` | Required with `header` and `oidc_jwt`: `roles: {admin: [<idp-group>]}`. `{admin: []}` means admin comes only from API tokens. |
| `api_tokens` | List of `{name, sha256, roles}`. Mint with `bifrost token new`; several can be active for rotation. |
| `BIFROST_TOKEN` | Client side: the token the CLI sends as `Authorization: Bearer`. |

### 2.5 Cost ceiling + rate limits

| Var | Default | Notes |
|---|---|---|
| `BIFROST_HANDOVER_MAX_COST_USD` | `10.0` | Tree-decomp generations exceeding this require `?force=true`. Roughly = 100 child Sonnet calls. |
| `BIFROST_HANDOVER_RATE_LIMIT_PER_MINUTE` | `6` | Per-cookie rate limit on `…/generate` calls. `0` disables. |
| `BIFROST_DOUBT_CLUSTER_THRESHOLD` | `0.85` | Cosine similarity threshold for the loop-back clusterer. Lower = more aggressive clustering. |

### 2.6 Integrations

| Var | Default | Used for |
|---|---|---|
| `BIFROST_SOURCES__JIRA__BASE_URL` | unset | Jira links and live fetch (`actions/jira_enrich.py`) |
| `BIFROST_SOURCES__JIRA__EMAIL` | unset | Jira API auth; set together with the token |
| `BIFROST_SOURCES__JIRA__API_TOKEN` | unset | Jira API auth (secret) |
| `BIFROST_SOURCES__JIRA__EPIC_LINK_FIELD` | unset | Older Jira only: `customfield_NNNNN` Epic Link; unset uses `parent` |
| `BIFROST_SOURCES__CONFLUENCE__BASE_URL` | unset | Self-hosted Confluence root for page links; Cloud always matches |
| `BIFROST_SLACK_ESCALATION_CHANNEL` | unset | When set, doubt escalations auto-post to this Slack channel/id through the Slack Web API (needs `BIFROST_SLACK_BOT_TOKEN`). Unset = on-disk artifact only. |

The old `JIRA_BASE_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN` names are rejected at startup.

### 2.7 Other

| Var | Default | Notes |
|---|---|---|
| `BIFROST_API_BASE_URL` | `http://localhost:8000` | Self-reference base URL for the API |
| `BIFROST_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `BIFROST_DISABLE_AUTO_ARCHIVE` | unset | Set to `1` in tests to disable the scheduled task auto-archiver |
| `BIFROST_EMBEDDING_CACHE` | unset | Optional cache path for sentence-transformers downloads |
| `BIFROST_HANDOVERS_DIR` | `docs/handovers` | Filesystem root for generated handovers (per-machine) |
| `BIFROST_TRACK` | `A` | Track A (Postgres) or Track B (Managed Agents). Track B is gated on Research Preview access. |

### If you remember nothing else
- In any shared / staging / production environment use `header` or `oidc_jwt` with `identity.roles`, and give scripts `bft_` tokens from `bifrost token new`.

---

## 3. Database operations

### 3.1 Migrations

```bash
# Apply all
uv run alembic upgrade head

# Roll back one
uv run alembic downgrade -1

# Show current revision
uv run alembic current

# New revision (autogenerate)
uv run alembic revision --autogenerate -m "your message"
```

**Expected output of `uv run alembic current`:**
```
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
001 (head)
```

Migration history: there is one baseline migration, `001_baseline.py`, which creates every table and enables pgvector. New migrations go after it. CI checks that Alembic has a single head.

### 3.2 Useful queries

```sql
-- Top-cost calls last 24h
SELECT model, phase, count(*), sum(estimated_cost_usd)::numeric(10,2)
FROM llm_cost_log
WHERE call_timestamp > now() - interval '1 day'
GROUP BY 1,2
ORDER BY 4 DESC LIMIT 20;

-- Stale handovers
SELECT scope_kind, scope_value, generated_at, freshness
FROM handover_docs
WHERE freshness IN ('stale', 'very_stale')
  AND archived_at IS NULL
ORDER BY generated_at;

-- Open collab sessions, oldest first
SELECT id, requirement_text, scope_kind, scope_value, created_at, state
FROM collaboration_sessions
WHERE state = 'open' AND closed_at IS NULL
ORDER BY created_at;

-- Tasks with stuck phases
SELECT t.id, t.state, p.phase, p.entered_at
FROM tasks t
JOIN task_phases p ON p.task_id = t.id
WHERE t.state = 'open'
  AND p.entered_at < now() - interval '1 hour'
ORDER BY p.entered_at;
```

### 3.3 Schema introspection

```bash
psql $BIFROST_DATABASE_URL -c "\d handover_docs"
psql $BIFROST_DATABASE_URL -c "\d collaboration_sessions"
psql $BIFROST_DATABASE_URL -c "\d entities"
```

**Expected output of `\d handover_docs` (truncated):**
```
                          Table "public.handover_docs"
    Column     |            Type             | Collation | Nullable | Default
---------------+-----------------------------+-----------+----------+---------
 id            | uuid                        |           | not null |
 scope_kind    | text                        |           | not null |
 scope_value   | text                        |           | not null |
 title         | text                        |           |          |
 body_md       | text                        |           |          |
 freshness     | text                        |           | not null | 'fresh'
 generated_at  | timestamp without time zone |           | not null | now()
 archived_at   | timestamp without time zone |           |          |
 body_search   | tsvector                    |           |          |
Indexes:
    "handover_docs_pkey" PRIMARY KEY, btree (id)
    "ix_handover_docs_body_search" gin (body_search)
    ...
```

---

## 4. Cost monitoring

### 4.1 The Costs page
`/costs` polls `GET /costs/summary` every 60s. Window selector: 7d / 30d / all. Charts: by model, by phase, by repo, daily series. → See [08 §13](./08-frontend-tour.md#13-costs--costspage).

### 4.2 The source table
Every Sonnet/Haiku call writes one row to `llm_cost_log` via `extraction/cost.py::CostTracker.record()`. Columns:

| Column | Notes |
|---|---|
| `model` | e.g., `claude-sonnet-4-6-20250514` |
| `phase` | `A` / `D` / `E` / `F` / `G` / `H` / `I` / `chat` / `clarifier` / `synthesizer` / `handover` / `task_*` |
| `input_tokens`, `output_tokens`, `cache_read_tokens` | Raw usage |
| `estimated_cost_usd` | CLI-reported `total_cost_usd` preferred; falls back to local estimate |
| `repo_id` | Nullable; chat / cross-repo calls have it null |
| `task_id` | Nullable; tied to extraction tasks |
| `call_timestamp` | UTC, tz-naive |

### 4.3 Divergence guard
If the local estimate diverges from CLI-reported cost by >50%, a warning is logged (`extraction/cost.py:191`). Do not ignore these — they catch both pricing-table drift and bugs in the local estimator.

### 4.4 Quick "where's the spend going" recipe

```bash
# Look at the most expensive single calls in the last hour
psql $BIFROST_DATABASE_URL -c "
  SELECT model, phase, repo_id, estimated_cost_usd, input_tokens, output_tokens
  FROM llm_cost_log
  WHERE call_timestamp > now() - interval '1 hour'
  ORDER BY estimated_cost_usd DESC
  LIMIT 20;
"
```

**Expected output (typical):**
```
                model                 | phase |  repo_id   | estimated_cost_usd | input_tokens | output_tokens
--------------------------------------+-------+------------+--------------------+--------------+---------------
 claude-sonnet-4-6-20250514           | D     | repo_abc12 |               0.42 |        38210 |          1820
 claude-sonnet-4-6-20250514           | G     | repo_abc12 |               0.31 |        24105 |          2110
 claude-sonnet-4-6-20250514           | E     | repo_abc12 |               0.19 |         9210 |          1402
 claude-haiku-4-5-20251001            | A     | repo_abc12 |               0.02 |         5210 |           302
 ...
```

### If you remember nothing else
- One LLM call = one `llm_cost_log` row. The Costs page is just a SELECT GROUP BY over this table.
- Phase D dominates. If a repo spend looks high, look at Phase D row count first.

---

## 5. Observability

### 5.1 Logs
- API: `/tmp/bifrost_server.log` (set by the restart script)
- UI dev: `/tmp/bifrost_ui.log`
- Set `BIFROST_LOG_LEVEL=DEBUG` for verbose Phase D / E / G traces

### 5.2 Health endpoints

```bash
# Detailed component health
curl -s http://localhost:8000/api/v1/health/detailed | python3 -m json.tool

# Claude CLI auth status
curl -s http://localhost:8000/api/v1/health/claude-cli | python3 -m json.tool
```

**Expected output of `/health/detailed`:**
```json
{
  "ok": true,
  "components": {
    "database":   { "ok": true, "latency_ms": 4 },
    "claude_cli": { "ok": true, "model": "claude-sonnet-4-6" },
    "event_bus":  { "ok": true, "subscribers": 3 },
    "embedding":  { "ok": true, "model": "all-MiniLM-L6-v2", "loaded": true }
  },
  "version": "0.4.2",
  "uptime_seconds": 1283
}
```

**Expected output of `/health/claude-cli`:**
```json
{ "authenticated": true, "model": "claude-sonnet-4-6", "expires_in_seconds": 3211 }
```

### 5.3 The ClaudeCliBanner
When the CLI loses auth (token expiry, deletion, OAuth refresh failure), the UI shows a red banner at the top: "Claude CLI not logged in." Run `claude auth login` and retry.

### 5.4 SSE channels (when debugging live)

```bash
# Watch a task as it runs
curl -N http://localhost:8000/api/v1/tasks/<task_id>/stream

# Watch a collaboration session
curl -N http://localhost:8000/api/v1/collab-sessions/<sid>/stream

# Watch a handover generation
curl -N "http://localhost:8000/api/v1/handover-docs/stream/domain/<did>"

# Watch a chat
curl -N http://localhost:8000/api/v1/conversations/<cid>/stream
```

**Expected output (watching a task):**
```
event: phase_started
data: {"phase":"A"}

event: phase_complete
data: {"phase":"A","duration_ms":2840}

event: phase_started
data: {"phase":"D"}

event: partial_output
data: {"phase":"D","components":12,"flows":0,"dependencies":0}
...
```

### If you remember nothing else
- `curl -N` against `/stream` endpoints is the fastest way to see what the backend is actually emitting.
- A red `ClaudeCliBanner` in the UI is always the first thing to fix — nothing else works without CLI auth.

---

## 6. Common incidents (symptom → diagnosis → remediation)

The table below is the operator's first-stop quick reference. Each row links to a detailed runbook entry below.

| Symptom | Probable cause | First fix | Detail |
|---|---|---|---|
| Extraction hangs at Phase G | Token budget overflow; subprocess hang | Pause + check log for `relationship prompt truncated` or `claude -p` stale process | [§6.10](#610-extraction-hangs-at-phase-g) |
| Handover doc shows "generating" forever | SSE `run_done` not emitted; worker died mid-tree-decomp | Check log for `child_done` count; if stuck, restart worker | [§6.11](#611-handover-doc-shows-generating-forever) |
| Chat returns "no sources found" | Scope picker excluded all repos OR no extraction yet | Verify `repos.freshness_state` for selected scope; trigger extraction if `unknown` | [§6.12](#612-chat-returns-no-sources-found) |
| Postgres connection pool exhausted | Long-running migration / leaked transaction | `SELECT pid, query FROM pg_stat_activity WHERE state='idle in transaction'`; restart API | [§6.13](#613-postgres-connection-pool-exhausted) |
| Empty page on a handover detail | `body_md` null; generation failed silently | [§6.1](#61-empty-page-on-a-handover-detail) |
| Clarifier did not fire | CLI auth lapsed; 45s timeout | [§6.2](#62-clarifier-didnt-fire) |
| SSE stalled | Reverse-proxy buffering | [§6.3](#63-sse-stalled) |
| Bulk handover refresh stuck | Per-cookie rate limit / cost ceiling | [§6.4](#64-bulk-handover-refresh-stuck) |
| Phase E hangs at 90s repeatedly | POJO slipped past `_has_flows_worth_extracting` | [§6.5](#65-phase-e-hangs-at-90s-timeout-repeatedly) |
| Extraction stuck at "5/200 (3 errors)" | Stale `claude -p` subprocess | [§6.6](#66-extraction-shows-phase-d-5200-files-3-errors-stuck-for-hours) |
| Discussion agent says "ready_to_plan: false" forever | Open clarifications unanswered | [§6.7](#67-discussion-agent-says-ready_to_plan-false-forever) |
| 401 `invalid_credentials` on every API call | A bearer value the server does not accept | [§6.8](#68-bearer-token-required-on-every-api-call) |
| Admin endpoints return 403 `auth.role_required` | Caller is a member | [§6.9](#69-admin-endpoints-return-403) |

### 6.1 Empty page on a handover detail
**Symptoms:** `/collaboration/handovers/<id>` shows "no body."

**Likely causes:**
1. The `body_md` field is null. Check: `SELECT id, length(body_md) FROM handover_docs WHERE id='<id>'`.
2. Generation failed silently. Check `bifrost_server.log` for `"used_fallback=True"` — that is the signal that the synthesis fell back to a stub.
3. Mermaid validation stripped *every* block. Less common but possible. Look for `stripped invalid mermaid` debug log lines.

**Fix:** Re-generate via `POST /handover-docs/<kind>/<value>/generate?force=true`. If still failing, check the cost ceiling — `BIFROST_HANDOVER_MAX_COST_USD` may be blocking.

### 6.2 Clarifier didn't fire
**Symptoms:** User posted a message in a collab session; no clarifier response appeared.

**Likely causes:**
1. CLI auth lapsed. Check `/health/claude-cli`.
2. Clarifier hit the 45s timeout. Check `bifrost_server.log` for `clarifier failed` warnings.
3. The fallback message was displayed but the user did not notice (it is a stub: "Got it — let me know more.").

**Fix:** Re-post the message. If it consistently fails, run `claude auth login`. Confirm the Sonnet model is configured (`BIFROST_AGENT_MODEL`).

### 6.3 SSE stalled
**Symptoms:** UI shows "agent running" but never updates. Browser network tab shows the SSE stream is open but no events.

**Likely causes:**
1. Reverse proxy / a proxy or VPN that buffers responses. Common with nginx without `proxy_buffering off`.
2. The producer side raised an exception after emitting `running` but before `done`. Check logs.
3. Browser tab was backgrounded; some browsers throttle background tabs.

**Fix:**
- For local dev: refresh the page; the SSE wrapper reconnects with backoff.
- For prod: set `proxy_buffering off` in the nginx location block for `/api/v1/*/stream`.

### 6.4 Bulk handover refresh stuck
**Symptoms:** `POST /handover-docs/bulk/refresh-stale` returns immediately but only some scopes regen.

**Likely causes:**
1. Per-cookie rate limit (`BIFROST_HANDOVER_RATE_LIMIT_PER_MINUTE=6` by default) is throttling subsequent regens.
2. Cost ceiling is rejecting tree-decomp scopes that lack `?force=true`.

**Fix:** Run with `BIFROST_HANDOVER_RATE_LIMIT_PER_MINUTE=0` (disable rate limit) and `?force=true` *if* you have already verified the cost is acceptable. Otherwise, run sequentially.

### 6.5 Phase E hangs at 90s timeout repeatedly
**Symptoms:** `Phase E retry 1/3 ... Phase E retry 2/3 ... Phase E failed` in the logs.

**Likely causes:**
1. Files being fed to Phase E are pure data (enums, POJOs, records). The `_has_flows_worth_extracting()` heuristic should skip them, but may have missed your idiom.
2. Genuine network / Anthropic-side slowness.

**Fix:**
- Check the file content the LLM is being shown. If it is pure data, file an issue and improve the heuristic.
- If transient: retry usually works.

→ See [06 §4](./06-extraction-pipeline.md#4-phase-e--flows-sonnet-parallel-3) for the heuristic source.

### 6.6 Extraction shows "Phase D: 5/200 files (3 errors)" stuck for hours
**Symptoms:** Counter increments very slowly or stops.

**Likely causes:**
1. The Claude CLI subprocess is hung. Check: `ps aux | grep "claude -p"` and look for stale processes.
2. Files exceeding 200K chars being skipped (errored) — that is expected behavior; the `errors` column counts these.

**Fix:**
- Kill stale subprocesses: `pkill -f "claude -p"`.
- Consider pausing the task (`POST /extraction-tasks/{id}/pause`) and resuming after diagnosis. The checkpoint preserves progress.

```bash
# Find stale claude -p processes
ps aux | grep "claude -p" | grep -v grep

# Kill them all
pkill -f "claude -p"

# Pause the running extraction (replace with task id)
curl -X POST http://localhost:8000/api/v1/extraction-tasks/<task_id>/pause

# After investigation, resume
curl -X POST http://localhost:8000/api/v1/extraction-tasks/<task_id>/resume
```

### 6.7 Discussion agent says "ready_to_plan: false" forever
**Symptoms:** Task stuck in `discussion` phase. Discussion always returns open questions.

**Likely causes:**
1. The user's input is genuinely ambiguous; clarifications are not being answered.
2. The blocking question's `blocks_state` is set on every turn, preventing advancement.

**Fix:** Answer the open clarifications (`POST /tasks/{id}/clarifications/{cid}/answer`), then re-run discussion.

### 6.8 "Bearer token required" on every API call
**Symptoms:** `/api/v1/*` calls return 401 `invalid_credentials`.

**Likely cause:** The request carries `Authorization: Bearer <value>` and the value is not a configured `bft_` token. A bearer that is not a `bft_` token is only accepted under `identity.provider: oidc_jwt`.

**Fix:** Mint a token with `bifrost token new`, add its entry to `api_tokens` in `bifrost.yaml`, restart, and send the token (the CLI reads `BIFROST_TOKEN`). A `401 identity_required` means no credentials arrived: under `none`, call from the same machine.

### 6.9 Admin endpoints return 403
**Symptoms:** Every `/admin/*` call, or a repo or extraction mutation, returns 403 with `auth.role_required`.

**Likely cause:** The caller is a member. Admin comes from a group listed in `identity.roles.admin`, a token with `roles: [admin]`, or the `none` provider's local operator.

**Fix:** Add the caller's IdP group to `identity.roles.admin`, or mint an admin token and probe with it.

```bash
bifrost token new --name ops --role admin   # add the printed entry to api_tokens, restart
curl -H "Authorization: Bearer <token>" http://localhost:8000/api/v1/admin/learning/stats
```


### 6.10 Extraction hangs at Phase G
**Symptoms:** Phase A-F complete in the log, but Phase G never finishes. Task stays in `running`.

**Likely causes:**
1. Token budget overflow — too many entities packed into one Sonnet call. Look for `relationship prompt truncated` warnings.
2. `claude -p` subprocess for Phase G hung.

**Diagnosis:**
```bash
# Are there stale subprocesses?
ps aux | grep "claude -p" | grep -v grep

# Tail the log for Phase G specifically
grep -i "phase G" /tmp/bifrost_server.log | tail -20
```

**Fix:**
- If subprocess is hung: `pkill -f "claude -p"` then `POST /extraction-tasks/{id}/resume`.
- If budget overflow: cap the entity count in Phase D first (rare; only on very large repos).

### 6.11 Handover doc shows "generating forever"
**Symptoms:** UI progress bar stuck at e.g. 23/30 for >10 minutes; no `run_done` event.

**Likely causes:**
1. Worker died mid-tree-decomp; `run_done` never emitted.
2. One child handover hung on `claude -p`.

**Diagnosis:**
```bash
# Check for stale claude -p subprocesses
ps aux | grep "claude -p" | grep -v grep

# Look at the recent generation events
psql $BIFROST_DATABASE_URL -c "
  SELECT generated_at, scope_kind, scope_value, freshness
  FROM handover_docs
  WHERE generated_at > now() - interval '20 minutes'
  ORDER BY generated_at DESC;
"
```

**Fix:**
- Kill stale subprocesses: `pkill -f "claude -p"`.
- Re-fire generation: `POST /handover-docs/<kind>/<value>/generate?force=true`. The estimator will surface any cost-ceiling concerns first.

### 6.12 Chat returns "no sources found"
**Symptoms:** Chat response shows "I could not find any relevant sources in the registered repos."

**Likely causes:**
1. The selected scope (a repo or domain) has not been extracted yet (`repos.freshness_state='unknown'`).
2. The scope picker excluded all candidate repos.
3. The router's top-K filter rejected every repo as low-relevance (rare on a real question, common on greetings / meta queries).

**Diagnosis:**
```bash
# Are repos extracted in the scope?
psql $BIFROST_DATABASE_URL -c "
  SELECT slug, freshness_state, last_extracted_at
  FROM repos
  WHERE freshness_state IN ('unknown', 'stale')
  ORDER BY freshness_state, slug;
"
```

**Fix:**
- For `unknown`: trigger extraction `POST /repos/{id}/extract`.
- For scope mismatch: clear the scope picker on the chat page and retry.

### 6.13 Postgres connection pool exhausted
**Symptoms:** Requests start returning 500s; log shows `QueuePool limit of size 5 overflow 10 reached`.

**Likely causes:**
1. A long-running migration leaked a transaction.
2. A test left an open connection (rare in prod).

**Diagnosis:**
```bash
psql $BIFROST_DATABASE_URL -c "
  SELECT pid, state, query_start, left(query, 80) AS query
  FROM pg_stat_activity
  WHERE datname = current_database()
    AND state IN ('idle in transaction', 'active')
  ORDER BY query_start;
"
```

**Fix:**
- Identify the offending pid (long `query_start`, `idle in transaction`).
- `SELECT pg_terminate_backend(<pid>)` on each offender.
- Restart the API to refresh the pool.

### If you remember nothing else
- `pkill -f "claude -p"` is the universal escape hatch for hung extractions.
- Almost every recovery path ends in `scripts/restart.sh` — the worker is not designed to self-heal mid-run.

---

## 7. Pre-flight checklist before each session

A 30-second sanity check before doing real work:

1. `claude auth status` returns "authenticated" (or run `claude auth login`).
2. `docker ps` shows the postgres container running.
3. `scripts/restart.sh --tail` and watch for `up after Xs`.
4. `curl http://localhost:8000/api/v1/health/detailed` returns `ok: true`.
5. UI loads at `http://localhost:5173` and shows no Claude-CLI banner.

**Expected output of `docker ps`:**
```
CONTAINER ID   IMAGE              STATUS         PORTS                    NAMES
abc123def456   pgvector/pgvector  Up 12 minutes  127.0.0.1:5432->5432/tcp bifrost-postgres-1
```

---

## 8. Things you'll wish you knew

- **The restart script unsets `BIFROST_EXTRACTION_DRY_RUN`.** Do not be surprised when env-set dry-run flags do not survive a restart — that is intentional.
- **`/tmp/bifrost-checkpoints` is per-machine.** Resume only works on the host that originally started the extraction. Cross-host resume = re-run from scratch.
- **`/tmp/bifrost-clones` can grow unbounded.** Production deploys should put this on a managed volume with cleanup.
- **The auto-archive job runs in-process.** If `BIFROST_DISABLE_AUTO_ARCHIVE=1` is set in tests but accidentally also in prod, terminal tasks pile up forever.
- **Postgres timestamps are `TIMESTAMP WITHOUT TIME ZONE`.** When comparing, strip tz from your `datetime.now(UTC)` first or asyncpg will reject the comparison. Source: `routes/costs.py:60-63`.
- **MiniLM downloads on first use.** First call to `compute_doubt_candidates` or any embedding path triggers the sentence-transformers download (~80MB). Slow request, then fast forever. Pre-warm in CI with `BIFROST_EMBEDDING_CACHE` set.
- **Track A vs Track B.** Every code path implements the `AgentRuntime` and `AgentStateBackend` interfaces so a future Track B (Managed Agents + Memory Stores + MCP) is a drop-in. Today the only `AgentRuntime` is the local Postgres-backed one.
- **No browser automation, ever.** Per the "Browser automation is forbidden" section of `CLAUDE.md` and the CI gate at `.github/workflows/pr-check.yml`. Bifrost extracts via `git clone` + `claude -p` subprocess; external data flows through MCP. If you find yourself reaching for puppeteer / playwright / chromium, you are on the wrong path.
- **The CLAUDE.md rules apply to the *codebase*, not just docs.** Rule 9 (Test before the next feature) is mandatory: every non-trivial feature gets a smoke test before moving on. Silent success ≠ success.
- **Slack auto-post needs a bot token.** Both `BIFROST_SLACK_BOT_TOKEN` and `BIFROST_SLACK_ESCALATION_CHANNEL` must be set. Either missing → the draft stays as an on-disk artifact.
