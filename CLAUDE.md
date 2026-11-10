# Working on Bifrost with Claude Code

**Purpose:** Keep sessions focused and verified. Read this file at the start of a session.

---

## First-time setup

If this is a fresh clone or the local dev environment isn't running,
the operator wants the repo set up with **least manual intervention**.
Do this:

1. **Read `RUNNING.md`** (the running guide). It documents every
   prereq, env var, port, troubleshooting case, and daily-flow command.

2. **Run the bootstrap script**:
   ```bash
   ./scripts/bootstrap.sh
   ```
   It automates 10 steps (verify tools, create `bifrost.yaml` from the
   example, `uv sync`, `npm install`, create `.env`, bring up Postgres,
   create the `bifrost_test` database, run migrations, `make up`,
   smoke-test). It's idempotent — safe to re-run after a `git pull` or
   to recover from a half-finished setup.

3. **The script halts with a clear error message when Docker Desktop is
   not running** → launch the macOS app, retry. Two logins matter:
   - `claude`: the script only runs `claude --version`, so a missing login
     shows up later as an extraction failure → `claude auth login`, retry
   - `gh` not authenticated → the script warns and continues; local repos
     work, GitHub-hosted repos need `gh auth login --scopes "repo,read:org"`

4. **After bootstrap finishes** the operator can open
   `http://localhost:5173`. Confirm with them which repo they want
   registered first.

**Do NOT recreate setup logic ad-hoc in a session.** If `RUNNING.md`
or `scripts/bootstrap.sh` is wrong or missing a step, fix THEM and
re-run the script — that way the next operator (and the next Claude
session) inherit the fix.

---

## Context hygiene

Large reads and long subagent transcripts fill the context window. Locate with `Grep`, then `Read` only the lines you need. Have subagents write their output to a file and return a one-line status. Finish one declared task per session.

---

## Session protocol

### Rule 0: Declare the session focus first
Every session starts with one sentence:
> "Session focus: [concrete task]. Expected output: [concrete deliverable]."

If the user hasn't stated this, ask: "What's the one thing this session should complete?" Do not begin work until this is answered.

### Rule 1: Find with search, read by range
- **Find files/symbols:** `Glob` + `Grep` — never `Read` speculatively
- **Read content:** Only with `offset` + `limit` targeting the specific section needed
- **Max direct reads per session:** 5 files. More than 5 means the task is too large — split it.

### Rule 2: Subagents write to a file
When delegating to a subagent:
1. Give the subagent an output file path
2. Tell it to write results there and return only a status line
3. Main Claude reads the output file only on error or review

Never let a subagent's full reasoning appear as inline tool results. The prompt must say: **"Write your complete output to `{path}`. Return only: `DONE: {path} — {one-line summary}` or `FAILED: {reason}`."**

### Rule 3: One file per subagent for spec work
When updating specs, each subagent gets exactly one spec file. Do not bundle. Do not give one agent "update S1, S4, and S8." Each gets one. This prevents subagent context overflow which then returns a massive error result to main context.

### Rule 4: Compact before the limit
Compact *before* hitting the wall under any of these conditions:
- Session has been active for 8+ exchanges
- More than 3 large files (>400 lines) have been read
- More than 4 subagents have returned results inline
- User's next task requires reading more than 2 new spec files

**How to proactively compact:** Tell the user what was accomplished, what's next, and what they should say to resume. Then wait for user to run `/compact`.

### Rule 5: Read this file first, specs on demand
At session start: read `CLAUDE.md` (this file). Do NOT preemptively read specs. Read specs only when the declared session focus specifically requires them.

### Rule 6: Read specs by section
When a task requires referencing a spec:
```
# DO: Find the section
Grep("pattern", "docs/archive/specs/orchestration.md")
# Then read only that range
Read("docs/archive/specs/orchestration.md", offset=240, limit=60)

# DON'T: 
Read("docs/archive/specs/orchestration.md")  # 1,400 lines → 6K tokens gone
```

### Rule 7: Split tasks that touch more than 3 specs
If completing a task requires touching more than 3 spec files, **stop and split the task**. Create a task list. Do one file per session exchange. This is not slower — it is the only way to finish.

### Rule 8: No large inline output
Never print more than 50 lines of generated content directly to the user in the chat. Write it to a file. Reference the file. This keeps the response token budget available for reasoning.

### Rule 9: Test before the next feature
**Mandatory.** Every time you build a non-trivial feature (new module, new route, new agent, new UI component), you MUST validate it end-to-end before moving on to build the next one:

- Backend changes: write a unit/integration test OR hit the API with curl and show real output.
- LLM subprocess / MCP changes: fire one real invocation and observe the response.
- UI changes: `npx vitest run` at minimum.
- Schema changes: run the migration and `\d <table>` or equivalent to prove it exists.

If the validation fails, stop. Fix it. Don't pile the next layer on top of a broken foundation — that turns a small bug into a compounding mess.

Silent success ≠ success. "I think this works" is not a validation. The cost of a 2-minute smoke test beats 2 hours of debugging a stack of features later.

---

---

## Project Orientation

**What Bifrost is:** An AI-powered engineering knowledge platform. Agents analyze codebases locally, extract knowledge (components, flows, dependencies), store it in PostgreSQL, and answer engineering questions with confidence-graded, sourced answers. The code runs as Python plus `claude -p` subprocesses. A managed-agents runtime is not built; code should stay behind the `AgentRuntime` and `AgentStateBackend` interfaces so one can be added.

**Layout:**
- `src/bifrost/` — backend (`research/`, `extraction/`, `manifest/`, `storage/`, `api/`, `cli/`)
- `alembic/versions/` — one baseline migration with a single head
- `ui/` — React frontend
- `tests/` — pytest suite; `scripts/` — bootstrap and gate scripts
- `docs/handbook/` — start with `02-architecture.md`; `docs/archive/specs/` — pre-build plans that may not match the code

**Checks:** [CONTRIBUTING.md](CONTRIBUTING.md) lists the test, ruff and UI commands. Also run the pyright gate and the pure leak gate:

```bash
uv run python scripts/pyright_gate.py
uv run python -m pytest -q -p no:cacheprovider --noconftest tests/test_leak_gate.py
```

**Enforced conventions:**
- `tests/test_neutral_names.py` and `tests/test_ui_neutral_copy.py` fail on organization-specific names in code or UI copy.
- Every test module that imports `tests/leak_terms.py` must be listed in `.github/workflows/leak-gate.yml`; `tests/test_leak_terms_wiring.py` checks this.
- Every name in the tree that starts with the `BIFROST` environment prefix must be a real setting or listed in `PROCESS_ENV_ONLY` (`src/bifrost/cli/unknown_settings.py`). `tests/test_unknown_settings.py` scans the tree.
- No `mcp__` tool names in `src/` outside `src/bifrost/cli/config_sections.py`.
- Keep a single Alembic baseline migration with one head. The CI gate compares heads.

---

## Browser automation is forbidden (formerly anti-pattern #10)

**Do not add browser automation (puppeteer, playwright, selenium, headless-chrome, webdriver, chromium) anywhere in the extraction or research path.** Bifrost extracts via `git clone` + `claude -p` subprocess; external data flows through MCP servers (`mcp__atlassian__*`, `mcp__github__*`, `mcp__claude_ai_Slack__*`). If a future task seems to require fetching a webpage:
- First confirm the data isn't reachable via an MCP tool the operator already has authenticated (`gh auth`, Atlassian OAuth, Slack OAuth).
- If genuinely no API path exists, use `httpx` / `requests` with explicit token auth — never a browser.
- Document why the MCP / API path didn't work in a comment at the call site.

The CI gate at `.github/workflows/pr-check.yml` will block any PR that introduces these packages or imports them in `src/`. Disabling that gate counts as violating this rule.
