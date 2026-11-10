# Running Bifrost locally

This guide gets a working Bifrost dev environment on your machine. It is written so you can hand it to Claude Code (or any coding agent) and have it run most of the steps.

If something here is wrong, open a pull request.

---

## Fastest path: the bootstrap script

```bash
git clone https://github.com/Amanborn007/bifrost.git
cd bifrost
./scripts/bootstrap.sh
```

The script runs ten steps: verify tools, create `bifrost.yaml` from the example, install Python and UI dependencies, create `.env`, start Postgres, create the `bifrost_test` database, run migrations, start the backend and UI, and smoke-test both. It is safe to re-run after a `git pull` or a half-finished setup.

It stops with a clear error when Docker Desktop is not running. Launch it, then retry.

The script warns and carries on if the GitHub CLI is not logged in. Local repos work without it. Registering a GitHub-hosted repo fails with a `gh auth login` message until you run `gh auth login --scopes "repo,read:org"`.

For the Claude Code CLI, the script only runs `claude --version` and prints a note. It does not check the login, so a missing login shows up later as an extraction failure. Run `claude auth login` first, or see the `OAuth token expired` entry under Troubleshooting.

When it finishes, open http://localhost:5173. The API is on http://localhost:8000.

To try the demo org instead of your own repos, copy its config first:

```bash
cp examples/acmeco/bifrost.yaml bifrost.yaml
./scripts/bootstrap.sh
uv run python scripts/seed_acmeco.py --bifrost-cmd "uv run bifrost" --register --extract
```

### Hand it to Claude Code

Paste this into a fresh Claude Code session in the cloned repo:

> Read `RUNNING.md` and `CLAUDE.md`, then run `./scripts/bootstrap.sh`. If it stops on a manual step, tell me exactly what to do and wait for me to confirm before retrying. When it reports complete, check that `/api/v1/repos` and http://localhost:5173 respond.

---

## 0. Prerequisites

| Tool | Version | Why | macOS install |
|------|---------|-----|---------------|
| Python | 3.12+ | Backend runtime | `brew install python@3.12` |
| uv | 0.4.30 or newer | Python package manager. CI pins 0.4.30; `uv.lock` was written by uv 0.9.25 | `brew install uv` |
| Node.js | 20+ | UI dev server | `brew install node` |
| Docker | recent | Runs Postgres with pgvector | `brew install --cask docker`, then launch Docker Desktop |
| Claude Code CLI | latest | Runs extraction and chat | `npm install -g @anthropic-ai/claude-code`, then `claude auth login` |
| gh CLI | 2.40+ | GitHub-hosted repos and the GitHub source; bootstrap warns if it is not logged in | `brew install gh`, then `gh auth login --scopes "repo,read:org"` |

Linux works for the backend and tests. Windows is untested.

Resources: 4 or more cores, about 8 GB of free RAM, and 10 GB of disk (each registered repo is cloned). A modern Chrome or Firefox is needed for the UI.

Network: outbound HTTPS to `api.anthropic.com` (the `claude` subprocess), `api.github.com`, `huggingface.co` (the sentence-transformers model downloads on first use and in tests), and whichever Jira, Confluence, Slack or Drive hosts you connect. No inbound ports.

---

## 1. Clone and install

```bash
git clone https://github.com/Amanborn007/bifrost.git
cd bifrost
uv sync --all-extras
cd ui && npm install && cd ..
```

---

## 2. Configuration files

Bifrost reads `bifrost.yaml` for organization settings and `.env` for the rest.

```bash
cp bifrost.example.yaml bifrost.yaml
```

Edit `bifrost.yaml` for your org. The sections are commented in the example file and summarised here:

| Section | What it controls |
|---|---|
| `org` | Display name of your organization. |
| `sources.github` | GitHub host and the orgs Bifrost may discover and clone from. |
| `sources.jira` | `base_url` used to build `<base_url>/browse/KEY-1` links. Empty turns links off. |
| `sources.local` | `allowed_roots`, the directories where local checkouts may be registered. An empty list allows any local path in dev and refuses all in production mode. |
| `mcp.tool_prefix` | Prefix of the tool names your MCP servers expose to `claude -p` (Atlassian, GitHub, Slack, Google Drive). |
| `manifest` | Which providers read ownership and runtime config from each repo, and which config files to scan. See [section 13](#13-configuration-reference). |
| `linking.internal_package_prefixes` | Package prefixes that mark a dependency as internal, for example `com.example.` or `@example/`. |
| `conventions` | Templates for Jira project, Confluence space and Slack channel, with `{TEAM}` and `{team}` filled from the owning team. |
| `identity.provider` | `none`, `header` or `oidc_jwt`. See [section 6](#6-identity). |

Startup fails on any `BIFROST_` name or top-level `bifrost.yaml` key that nothing reads, and suggests the closest valid name. A few names are read only from the process environment (`BIFROST_CONFIG`, `BIFROST_HANDOVERS_DIR` and similar); put one in `.env` and startup fails. The lookup rule:

- `BIFROST_CONFIG=<path>` uses that file, and it must exist.
- `BIFROST_CONFIG=""` loads no file.
- If unset, `./bifrost.yaml` is used when present.

Precedence, highest first: init arguments, environment variables, `.env`, `bifrost.yaml`, defaults. Any value can be overridden from the environment with `__` nesting, for example `BIFROST_SOURCES__JIRA__BASE_URL=https://example.atlassian.net`.

The minimum `.env`:

```bash
cat > .env <<'EOF'
# Postgres, matching the docker-compose defaults.
BIFROST_DATABASE_URL=postgresql+asyncpg://bifrost:bifrost_dev@localhost:5432/bifrost

# The local operator identity (used when identity.provider is none).
BIFROST_OPERATOR_EMAIL=you@example.com

# How many extractions run at once. Raise it if you have Claude rate-limit headroom.
BIFROST_MAX_CONCURRENT_EXTRACTIONS=2

# Skip the real Claude subprocess for fast iteration.
# BIFROST_EXTRACTION_DRY_RUN=1
EOF
```

Every variable starts with `BIFROST_` and is read by pydantic-settings. `.env.example` lists them all. Restart the backend (`make backend`) after a change.

---

## 3. Start Postgres

```bash
docker compose up -d
docker inspect --format='{{.State.Health.Status}}' bifrost-postgres   # wait for: healthy
```

Compose creates the database `bifrost`. The test suite uses a separate database, `bifrost_test`. The test fixtures create it on the first run.

---

## 4. Run migrations

```bash
uv run alembic upgrade head
```

There is a single baseline migration. It creates the tables and enables the pgvector extension. If it fails because the extension is missing (you are using your own Postgres), run:

```bash
docker exec -it bifrost-postgres psql -U bifrost -d bifrost -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

---

## 5. Start the servers

```bash
make up
```

This runs `scripts/dev.sh up`, which starts the backend on :8000 (log in `/tmp/bifrost-backend.log`) and the Vite UI on :5173 (log in `/tmp/bifrost-ui.log`), then waits for both to answer.

```bash
make status | logs | logs-backend | logs-ui
make restart     # stop and start both
make backend     # restart only the backend
make ui          # restart only the UI
make health      # hit both health endpoints
make down        # stop both
```

---

## 6. Identity

With `identity.provider: none` (the default) there is no login. Bifrost treats every request from this machine as the local operator, whose email is `BIFROST_OPERATOR_EMAIL`, and the operator is an admin. If the UI chip in the top right shows no identity, set that variable in `.env` and run `make restart`.

`none` only answers loopback clients (`127.0.0.1` and `::1`), so the backend must listen on `127.0.0.1`, as `scripts/dev.sh` does. A request from any other address gets `401 identity_required`. Do not bind it to `0.0.0.0` or put a proxy in front of it; for a shared deployment use `header` or `oidc_jwt`.

For a shared deployment set `identity.provider` to `header` (a reverse proxy sets the principal headers) or `oidc_jwt` (Bifrost verifies a bearer JWT against your provider's JWKS). Both need `identity.roles` to say which IdP groups are admins (`roles: {admin: [your-group]}`, or `{admin: []}` for none); everyone else is a member. With `header`, run uvicorn with `--no-proxy-headers` so the peer address Bifrost checks against `identity.header.trusted_proxy_cidrs` is the proxy's, not a forwarded client address.

`header` mode needs `identity.trusted_proxy: true`, and only believes the headers named under `identity.header` when the peer address is inside `identity.header.trusted_proxy_cidrs` (loopback by default). From any other peer the headers are ignored and stripped, so add your proxy's range there if it runs on another host. `oidc_jwt` needs `identity.oidc.jwks_url`, `issuer` and `audience`. Browser `POST`, `PUT`, `PATCH` and `DELETE` requests (those with an `Origin`, `Sec-Fetch-Site` or cookie) must come from a `BIFROST_CORS_ORIGINS` origin and carry `X-Requested-With: bifrost-ui`, which the UI sends. Otherwise they get 403. Bearer-token clients are exempt.

Scripts and the CLI authenticate with an API token. Run `uv run bifrost token new --name ci`, add the printed `api_tokens` entry to `bifrost.yaml`, restart, and export the token as `BIFROST_TOKEN` where the CLI runs.

The chip shows the source of the identity: `ENV` for `BIFROST_OPERATOR_EMAIL`, `DEMO` for the demo principal, `SSO` for a trusted-proxy header, `OIDC_JWT` for a verified bearer token. A request to an admin route as a member gets `403 auth.role_required`. If you get `401 Unauthorized` mid-session, the identity no longer resolves: re-set `BIFROST_OPERATOR_EMAIL` and run `make backend`, or sign in again through your proxy.

---

## 7. Register your first repository

In the UI: open Overview, click Register repository, paste a Git URL you can access (HTTPS or SSH), pick a domain, and submit. Or from the CLI:

```bash
uv run bifrost repo add https://github.com/<owner>/<repo>.git
uv run bifrost repo list
```

Registering through the API or `bifrost repo add` only records the repo and syncs its ownership manifest. It does not extract. The UI dialog does queue an extraction when its "extract immediately" box is ticked (the default). From the CLI, run `uv run bifrost extract run <repo-id>` (the full id is in the `GET /api/v1/repos` response; `bifrost repo list` truncates it). The backend then clones the repo and extracts in the background, which takes 3 to 15 minutes depending on size. Progress shows on the Overview page.

To register local checkouts, add their parent directories to `sources.local.allowed_roots` in `bifrost.yaml`. A path outside those roots is refused.

To pull in every repository of a team, call `POST /api/v1/domains/{id}/discover-repos` with `{"team": "DLVR", "dry_run": true}`. It searches your configured GitHub orgs for repos whose manifest names that team, then reports which would be added and which existing repos would be linked to the domain. Send `"dry_run": false` to apply it.

---

## 8. Ask a question

When a repo shows `fresh` on Overview, open Chat and ask something about it, for example "How does authentication work in this repo?". The research pipeline scopes the question to repos, fans out to per-source agents (code, Jira, Confluence, GitHub, Slack, Google Drive), and writes a cited answer with confidence labels. A turn takes 30 to 90 seconds. Every call's cost is written to `llm_cost_log` and shown on the Costs page. The README demo section has the one full benchmark-run total.

To build the service graph from your catalog files, call `POST /api/v1/admin/index-services` (needs the admin role; the local operator under `identity.provider: none` has it). It reads `catalog-info.yaml`, `CODEOWNERS` and the `*_HOST` and `*_TOPIC` keys in each repo's config files.

---

## 9. Run the tests

```bash
uv run pytest tests/ -q --ignore=tests/e2e     # needs Postgres
cd ui && npx vitest run                        # UI unit tests
```

`npx tsc -b` currently reports 27 type errors, so `npm run build` exits non-zero. It is left out of the check above until those are fixed.

The backend tests connect to `postgresql+asyncpg://bifrost:bifrost_dev@localhost:5432/bifrost_test`. Set `TEST_DATABASE_URL` to use another database.

---

## 10. Daily flow

```bash
docker compose up -d     # Postgres, if it is not already up
make up                  # backend and UI
make down                # when done; Postgres keeps running
```

`uv sync` creates `.venv` in the repo root. pyright reads `venvPath = "."` and `venv = ".venv"` from `pyproject.toml`, so a venv elsewhere fails `tests/e2e/test_quality_gates.py::test_type_safety_gate`.

After a `git pull`:

```bash
uv sync --all-extras
cd ui && npm install && cd ..
uv run alembic upgrade head
make restart
```

---

## 11. Resetting state

```bash
# Wipe the database only.
docker compose down -v && docker compose up -d
uv run alembic upgrade head

# Also wipe cloned repos and logs.
rm -rf ~/.bifrost /tmp/bifrost-clones /tmp/bifrost-checkpoints
rm -f /tmp/bifrost-*.log /tmp/bifrost-*.pid
```

`docker compose down -v` drops the `pgdata` volume. Without `-v` your data survives a container restart.

---

## 12. Ports and paths

| Port | Service | Config |
|------|---------|--------|
| 5432 | Postgres | `docker-compose.yml` |
| 8000 | API | `scripts/dev.sh` |
| 5173 | Vite UI | `ui/vite.config.ts` |

| Path | Contents | Safe to delete |
|------|----------|----------------|
| `/tmp/bifrost-clones` | Cloned repos (the `clone_dir` setting) | Yes, they re-clone |
| `/tmp/bifrost-checkpoints` | Extraction checkpoints | Yes, extraction restarts |
| `/tmp/bifrost-*.log`, `*.pid` | Server logs and PID files | Yes |
| `docs/handovers` (`BIFROST_HANDOVERS_DIR`) | Handover documents | No |
| `pgdata` Docker volume | Postgres data, including spend in `llm_cost_log` | Only for a clean database |

The SSE event buffers live in memory and are lost on restart.

---

## 13. Configuration reference

### Slack escalation

Escalating a collaboration doubt can also post the draft to Slack. It is off until both `BIFROST_SLACK_ESCALATION_CHANNEL` (a name like `#eng-handovers` or a channel id) and `BIFROST_SLACK_BOT_TOKEN` (a bot token with `chat:write`) are set. The post goes to that channel through the Slack Web API, with no LLM call and no MCP. If Slack fails twice, the resolve request returns 502 with error code `slack.escalation.post_failed` (or `slack.escalation.permalink_failed` when the message was posted but the link lookup failed).

### Manifest providers

`manifest.providers` is an ordered list. Each provider reads one file from a repo and fills the ownership fields:

- `backstage` reads `catalog-info.yaml`.
- `codeowners` reads `CODEOWNERS`.
- `generic_yaml` reads a YAML file at a path you choose, with a field map in `bifrost.yaml`.
- `none` turns manifest reading off.

Runtime settings come from the files in `manifest.config_globs` (`config/*.env`, `docker-compose*.yml`, Helm values). Keys ending in `_HOST`, `_URL` or `_ENDPOINT` become `calls_service` edges. Keys ending in `_TOPIC` or `_QUEUE` become `publishes_to` or `shares_topic` edges. Both need the name or value to match another repo. Backstage `dependsOn` entries become `depends_on` edges.

### Roles and API tokens

There are two roles. Every signed-in user is a `member`: they can browse, ask questions and chat. `admin` is needed for everything that changes the corpus, spends in bulk, runs code on the host or writes to Jira (registering repos and domains, extraction, indexing, `/admin/*`, task execute and PR creation). See [SECURITY.md](SECURITY.md#task-execution) before enabling task execution on a shared host.

With `header` or `oidc_jwt` you must set `identity.roles`, which maps IdP groups to `admin`:

```yaml
identity:
  roles:
    admin: [eng-platform-admins]
```

`roles: {admin: []}` is valid and means admin comes only from API tokens. A group named `bifrost-admin` has no special meaning. With `provider: none` the local operator is an admin.

Scripts and the CLI use API tokens. Run `bifrost token new --name ci --role member`, store the printed `bft_...` token, and add the printed entry (name, sha256, roles) under `api_tokens` in `bifrost.yaml`. Clients send it as `Authorization: Bearer <token>`; the CLI reads it from `BIFROST_TOKEN`. Keep two entries active while you rotate. Any other bearer value is a JWT under `oidc_jwt` and a `401 invalid_credentials` everywhere else.

### Production mode

Set `BIFROST_PRODUCTION_MODE=true` for any deployment other than a laptop. The app then refuses to start unless these are set:

- `BIFROST_SESSION_SIGNING_KEY`, a stable HMAC key. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `BIFROST_DATABASE_URL`, an explicit Postgres URL (the local-dev default is rejected).
- `BIFROST_CORS_ORIGINS`, a comma-separated allowlist (the local-dev default is rejected).
- A verified identity path: `identity.provider: header` with `identity.trusted_proxy: true`, or `identity.provider: oidc_jwt`. `BIFROST_DEMO_MODE` must not be true.

Set `identity.roles` (see Roles and API tokens above) and, for scripts, `api_tokens`. `.env.example` lists every variable with its default.

---

## Troubleshooting

### `docker compose up -d` says port 5432 is in use

A local Postgres is on the default port. Stop it (`brew services stop postgresql@16`) or set `POSTGRES_PORT=5433` in `.env` and update `BIFROST_DATABASE_URL` in `.env` to use port 5433. The test suite ignores the repo-root `.env`, so pass the test database on the command line: `TEST_DATABASE_URL=postgresql+asyncpg://bifrost:bifrost_dev@localhost:5433/bifrost_test make test`.

### The backend exits with `ConnectionRefusedError`

Postgres is up but not ready. Wait five seconds and `make restart`. If it persists, read `make logs-backend`.

### The UI says it cannot reach the API

The backend is not running (`make status`), or the Vite proxy is not forwarding `/api` to `localhost:8000` (check `ui/vite.config.ts`).

### Every request returns 401

Identity is not resolving. With `identity.provider: none`, set `BIFROST_OPERATOR_EMAIL` in `.env` and `make restart`, and call the backend from the same machine (only loopback clients are served). With `header`, check that the proxy's address is inside `identity.header.trusted_proxy_cidrs`. With `oidc_jwt`, check that its settings match your issuer. A bearer value that does not start with `bft_` is only accepted under `oidc_jwt`; elsewhere it is a `401 invalid_credentials`.

### `make up` says the PID file is stale

```bash
rm -f /tmp/bifrost-*.pid && make up
```

### Extraction stalls for a long time

Claude rate limits. Check `make logs-backend | grep -i 'rate.limit'`, set `BIFROST_MAX_CONCURRENT_EXTRACTIONS=1` in `.env`, and `make restart`.

### The `claude` subprocess fails with "OAuth token expired"

Run `claude auth login` again. Bifrost uses your local Claude Code session and has no API key. If login succeeds but extraction still fails, make sure the `claude` on `PATH` is the one you logged in with (no `nvm` or `asdf` shadowing).

### The GitHub source returns 401 or "missing scopes"

```bash
gh auth login --scopes "repo,read:org"
gh auth status
```

### Vite starts on a different port

Something else holds 5173. Find it with `lsof -i :5173 -P -n`, stop it, and run `make ui`.

### A chat answer shows as one block of unformatted text

Answers render client-side. Open the browser console and look for an error from the prose component or the diagram renderer. A malformed diagram block from the model is the usual cause. Refresh the page.

---

## What lives where

```
bifrost/
  src/bifrost/        backend (api, research, extraction, manifest, actions, storage)
  alembic/versions/   the baseline migration
  tests/              pytest suites
  ui/src/             React frontend (pages, components, lib)
  examples/acmeco/    demo org
  docker-compose.yml  Postgres with pgvector
  scripts/dev.sh      backend and UI lifecycle
  Makefile            thin wrappers around dev.sh
  bifrost.example.yaml
  CLAUDE.md           rules for Claude Code sessions
  RUNNING.md          this file
```

## Command reference

```bash
make up | restart | status | health | logs | down

uv run pytest tests/ -q --ignore=tests/e2e
cd ui && npx vitest run

uv run alembic upgrade head
uv run alembic revision -m "name"

docker compose up -d
docker compose down        # keeps data
docker compose down -v     # wipes data
```
