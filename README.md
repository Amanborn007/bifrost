# Bifrost

## The problem

In a codebase split across many repositories, a question like "what breaks if `signing-keys` rotates its key" is hard to answer. The answer is scattered across code, tickets, docs and chat, and the person asking usually does not own the code. Two moments where that happens: an engineer paged for a service another team owns, and a team that receives a system after a reorg and has to learn what it calls and what calls it. Bifrost clones the repositories, extracts components, flows and dependencies into Postgres with pgvector, and answers with `file:line` citations and a confidence label.

It also drafts structured handover documents, so one team can hand a system to another against a document instead of an ad-hoc chat thread.

Code search tools such as Sourcegraph answer where something is. Knowledge tools such as Glean and Unblocked search code, tickets and chat. Bifrost builds its own graph across repositories and walks it to answer blast-radius questions, and it runs on your machine with your Claude login. I have not run it against those tools. [Roadmap section 1](docs/roadmap.md#1-verdict) surveys 20 systems from product pages and papers, and [section 4](docs/roadmap.md#4-method-and-limits) says the survey ran no experiment. Section 5 has the comparison tables.

All LLM calls go through the `claude -p` CLI using your own Claude Code login. There is no API key and no gateway to configure.

Status: early prototype, one author. It is fine for trying on your own repos. It is not hardened for a multi-user deployment yet.

---

## See it work (AcmeCo demo)

`examples/acmeco` holds five small fictional services (a webhook delivery platform in two regions). `scripts/seed_acmeco.py` turns them into git repos with a fixed commit history, so the SHAs are the same on every machine.

Step 1 is free and needs only git and Python 3. Step 2 needs Docker, Python 3.12+, `uv`, Node 20+, the Claude Code CLI and the `gh` CLI installed. The `gh` login is optional for the demo: bootstrap warns if it is not logged in and carries on, because the demo repos are local paths. GitHub-hosted repos need `gh auth login --scopes "repo,read:org"`. [RUNNING.md](RUNNING.md#0-prerequisites) has install commands.

```bash
git clone https://github.com/Amanborn007/bifrost.git
cd bifrost

# Step 1, free.
python3 scripts/seed_acmeco.py

# Step 2, spends Claude usage. Registers the repos and extracts them one at a time.
claude auth login             # once, opens a browser
cp examples/acmeco/bifrost.yaml bifrost.yaml   # the demo org's config
./scripts/bootstrap.sh        # deps, .env, Postgres, migrations, servers
uv run python scripts/seed_acmeco.py --bifrost-cmd "uv run bifrost" --register --extract
```

Step 1 prints one line per repo, which are the paths to register:

```text
seeded <tmp>/acmeco/webhook-api
seeded <tmp>/acmeco/dispatcher
seeded <tmp>/acmeco/delivery-gateway
seeded <tmp>/acmeco/signing-keys
seeded <tmp>/acmeco/usage-metering
```

Open http://localhost:5173. The API is on http://localhost:8000. Extraction and every chat turn spend Claude usage. Each call's cost is written to `llm_cost_log`, so the Costs page shows the real total for your run. I have not recorded a total for AcmeCo. The nearest figures are an estimate of $15 to $50 for one 200-file repo ([handbook 06](docs/handbook/06-extraction-pipeline.md)), where the five AcmeCo repos are 59 small files in total, and a measured $44 to $51 for a full A/B/C benchmark run in late 2026 (12 questions in runs A and B and the 9 with a paraphrase in run C, so 33 chat turns; [roadmap section 1](docs/roadmap.md#1-verdict)).

The cheapest path to one answer: the benchmark scopes the question below to `delivery-gateway` and `signing-keys` (`scope_repo_names` in `docs/benchmarks/acme.yml`). Run step 1 and step 2 up to `./scripts/bootstrap.sh`, then run `uv run bifrost repo add <path>` and `uv run bifrost extract run <repo-id>` for those two repos only, in place of the seed command, using the paths step 1 prints. I have not run the demo with only two repos.

When the repos read `fresh` on Overview, open Chat and ask:

> How are webhook payloads signed, which service does the signing, and what happens to the old secret when a key is rotated?

A good answer names `delivery-gateway` and `signing-keys`, says the signature is HMAC, and says how long the previous key stays valid after rotation (`ROTATION_GRACE_HOURS`). Claims carry `file:line` footnotes and the answer carries a confidence label. `docs/benchmarks/acme.yml` (`q7-acme-signing-key-flow`) lists the expected facts, and 11 more questions sit beside it, including the service map in `q1-acme-service-map`.

There is no zero-spend demo, and no screenshot or recorded answer is committed, so you cannot see the output without spending. Seeding and the checks-only test path are free, and the expected facts above show what a good answer must contain. `BIFROST_EXTRACTION_DRY_RUN=1` mocks extraction only, and chat still calls Claude.

---

## Quickstart on your own repos

```bash
cp bifrost.example.yaml bifrost.yaml          # edit it for your org
./scripts/bootstrap.sh
uv run bifrost repo add <git-url>
uv run bifrost extract run <repo-id>          # repo add does not extract; it prints the repo id
```

Open http://localhost:5173. Registering from the Overview dialog extracts by default ([RUNNING.md section 7](RUNNING.md#7-register-your-first-repository)). Bootstrap only warns if `gh` is not authenticated. To run only the tests and checks, with no `claude` and no `gh`, use the [checks-only path](CONTRIBUTING.md#run-the-checks-only).

---

## How it works

Paths are under `src/bifrost/` unless noted. Startup tasks, extraction phases and data flows are in [handbook 02](docs/handbook/02-architecture.md).

| Component | Path | What it does | Talks to |
|---|---|---|---|
| API and SSE | `api/` | FastAPI app. Startup launches the extraction worker and background tasks. | Postgres, the UI over EventSource |
| Auth | `api/auth/` | Identity providers `none` (loopback only), `header` and `oidc_jwt`. CSRF checks, `admin` and `member` roles, `bft_` API tokens. | Your IdP's JWKS endpoint, for `oidc_jwt` only |
| LLM client and extraction | `extraction/` | Every model call is a `claude -p` subprocess. Extraction runs LLM phases with checkpoint resume and redacts secrets before prompting. | `git clone`, `claude -p`, Postgres |
| Manifest and service graph | `manifest/` | Reads `catalog-info.yaml`, `CODEOWNERS` or a configured YAML file for ownership. `env_miner.py` mines host and topic keys from config files, and those edges are what a blast-radius question walks. | Repo files, Postgres |
| Research | `research/` | Scopes the question, plans, runs per-repo and per-source agents, then synthesises an answer with confidence tiers and footnotes and runs an adversarial verifier. | `claude -p`, MCP servers, pgvector |
| Embeddings | `research/embeddings.py`, `research/graph_retrieval.py` | `all-MiniLM-L6-v2`, 384 dimensions, on CPU. Vectors find seed entities and a bounded walk over the graph builds the context. | pgvector; huggingface.co on first model download |
| Collaboration | `collaboration/` | Handover documents, clarifier, synthesiser, matching and Slack escalation. | Slack Web API (no model in that path), `claude -p` |
| Actions | `actions/` | Chat turns into a task, a plan, an execution and a verification, in a git worktree. | A git worktree on the host, `gh` |
| Agents and orchestration | `agents/`, `orchestration/` | A second research pipeline behind `POST /api/v1/ask`. Only tests call it, and chat uses `research/`. | `claude -p`, Postgres |
| Adapters and core | `adapters/`, `core/` | `SourceAdapter` base class with Git and Jira adapters, domain models and Fernet credential encryption. | `git`, the Jira REST API |
| Storage | `storage/`, `alembic/versions/001_baseline.py` | SQLAlchemy 2 async models and one baseline migration. | Postgres 16 with pgvector 0.8.0 |
| CLI | `cli/` | The `bifrost` command, a thin client of the API. | The API |
| UI | `ui/` | React 19, Vite, TanStack Query, Tailwind 4. | The API through the Vite proxy |

Where to start in the code: `extraction/cli_client.py` (the `claude -p` wrapper), `extraction/pipeline.py` (extraction phases) and `run_research` in `research/coordinator.py` (the chat path). The topology diagram is in [handbook 02 section 1](docs/handbook/02-architecture.md#1-topology-at-a-glance) and the trust boundary is in [section 8](docs/handbook/02-architecture.md#8-identity-auth-and-security-boundary). Where state lives is in [RUNNING.md section 12](RUNNING.md#12-ports-and-paths).

---

## Testing

```bash
make test                                   # backend; needs Postgres
cd ui && npx vitest run                     # UI
```

Most backend tests need Postgres, because the session fixture in `tests/conftest.py` migrates a test database, creating `bifrost_test` if it is missing. `BIFROST_SKIP_DB_MIGRATIONS=1` skips that step for pure-logic tests, and the leak gate command in [CONTRIBUTING.md](CONTRIBUTING.md#run-the-checks-only) needs no database. Counts are in [docs/testing.md](docs/testing.md).

Three of the edge cases with a test each:

- `tests/test_git_ssrf.py::TestProductionOptOut::test_opt_in_refused_in_production`: the private-address opt-in for git URLs is refused in production mode.
- `tests/test_csrf.py::test_cookie_auth_without_header_rejected`: a cookie-authenticated mutation without `X-Requested-With: bifrost-ui` gets 403, and with it gets 200.
- `tests/test_unknown_settings.py::test_error_suggests_the_closest_name`: a mistyped `BIFROST_` variable fails startup with "did you mean" and the nearest valid name.

[docs/testing.md](docs/testing.md) has the other five edge cases and the benchmark. The benchmark scorer uses regex and substring matching only, and three identical baseline runs spread by 0.007, so a smaller gain cannot be read.

---

## Known limitations

- The task executor, verifier and debugger run `claude -p` with `bypassPermissions`, and `HOME` is passed through. See [SECURITY.md](SECURITY.md#task-execution).
- One process. The SSE bus is process-local, so a second worker would not see the first one's events ([handbook 02 section 10](docs/handbook/02-architecture.md#10-open-architectural-questions)).
- Extraction is LLM-only. The graph has about 0.12 edges per entity (docstring in `research/graph_retrieval.py`). MiniLM truncates input at 128 tokens ([roadmap section 6.1](docs/roadmap.md#61-current-state)).
- `npx tsc -b`, which `npm run build` runs, reports 27 type errors on 2026-11-10 after a fresh `npm ci`. The CI step `npx tsc --noEmit` checks nothing, because `ui/tsconfig.json` has `"files": []`.
- The blast-radius benchmark takes its ground truth from the same graph it walks, so it measures the walk, not real impact ([roadmap section 6.2](docs/roadmap.md#62-the-question-pipeline-and-its-gaps)).
- Not hardened for multiple users. Windows is untested.

---

## Docs map

- What is built: [docs/handbook/](docs/handbook/README.md). Start at [02 architecture](docs/handbook/02-architecture.md).
- Decisions, failures and next steps: [docs/DECISIONS.md](docs/DECISIONS.md).
- Tests, edge cases and the benchmark: [docs/testing.md](docs/testing.md).
- Research and experiment plan: [docs/roadmap.md](docs/roadmap.md).
- History: [CHANGELOG.md](CHANGELOG.md).
- Operating it, prerequisites and configuration: [RUNNING.md](RUNNING.md). Contributing and the development commands: [CONTRIBUTING.md](CONTRIBUTING.md).
- Written before or during the build, so they may not match the code: [docs/archive/](docs/archive/README.md).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Security reports go through [SECURITY.md](SECURITY.md).
