#!/usr/bin/env bash
# Bifrost first-time setup automation.
#
# What it does (in order, idempotent — safe to re-run):
#   1. Verify required tools are installed; print install hints for missing ones
#   2. Check that bifrost.yaml exists (copy bifrost.example.yaml if not)
#   3. uv sync                                 — Python deps
#   4. cd ui && npm install                    — UI deps
#   5. Create .env from template if absent     — prompts only for email
#   6. docker compose up -d                    — Postgres
#   7. Wait for Postgres healthcheck
#   8. uv run alembic upgrade head             — migrations
#   9. make up                                 — backend + UI
#  10. Smoke /api/v1/repos                     — sanity
#
# Manual interventions (script will stop and tell you, then exit non-zero):
#   - Docker Desktop not running (we can't launch a macOS app from a script)
#
# Warned about, not enforced:
#   - claude CLI login (only `claude --version` is checked) → `claude auth login`
#   - gh CLI login (only GitHub-hosted repos need it) → `gh auth login --scopes "repo,read:org"`
#
# Usage:
#   ./scripts/bootstrap.sh
#
# Re-running: every step short-circuits when already complete. Safe to
# run after `git pull` to bring everything back in sync.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# ── ANSI colors ───────────────────────────────────────────────────
if [[ -t 1 ]]; then
    C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'
    C_DIM=$'\033[90m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
    C_OK= C_WARN= C_ERR= C_DIM= C_BOLD= C_RESET=
fi
ok()    { echo "${C_OK}✓${C_RESET} $*"; }
warn()  { echo "${C_WARN}!${C_RESET} $*"; }
err()   { echo "${C_ERR}✗${C_RESET} $*" >&2; }
step()  { echo ""; echo "${C_BOLD}▸ $*${C_RESET}"; }
dim()   { echo "${C_DIM}  $*${C_RESET}"; }

fatal() {
    err "$*"
    echo ""
    echo "Bootstrap aborted. Fix the above, then re-run ./scripts/bootstrap.sh"
    exit 1
}

# ── Step 1: Verify tools ──────────────────────────────────────────

step "Step 1/10: Verifying required tools"

declare -a MISSING=()
check_tool() {
    local name="$1" cmd="$2" install_hint="$3"
    if command -v "$cmd" >/dev/null 2>&1; then
        ok "$name installed ($(command -v "$cmd"))"
    else
        MISSING+=("$name: $install_hint")
    fi
}

check_tool "uv"           uv      "brew install uv"
if command -v uv >/dev/null 2>&1; then
    if py312=$(uv python find '>=3.12' 2>/dev/null); then
        ok "Python 3.12+ available ($py312)"
    else
        MISSING+=("Python 3.12+: uv python install 3.12")
    fi
fi
check_tool "Node 20+"     node    "brew install node"
check_tool "Docker"       docker  "brew install --cask docker  (then launch Docker Desktop)"
check_tool "Claude CLI"   claude  "npm install -g @anthropic-ai/claude-code  (then 'claude auth login')"
check_tool "gh CLI"       gh      "brew install gh  (then 'gh auth login --scopes \"repo,read:org\"')"

if [[ ${#MISSING[@]} -gt 0 ]]; then
    err "Missing tools:"
    printf '  - %s\n' "${MISSING[@]}"
    fatal "Install them, then re-run. To run only the tests and checks, no claude or gh needed, see CONTRIBUTING.md."
fi

# Docker daemon reachable?
if ! docker info >/dev/null 2>&1; then
    fatal "Docker is installed but the daemon isn't reachable. Launch Docker Desktop and retry."
fi
ok "Docker daemon reachable"

# Claude CLI authenticated?
if ! claude --version >/dev/null 2>&1; then
    fatal "Claude CLI version check failed. Reinstall: npm install -g @anthropic-ai/claude-code"
fi
# We can't directly detect "logged in" without an actual call — surface a soft warn.
dim "Note: if extraction later fails with 'OAuth token expired', run: claude auth login"

# gh CLI authenticated?
if gh auth status >/dev/null 2>&1; then
    ok "gh CLI authenticated"
else
    warn "gh CLI not authenticated. Local repos and the AcmeCo demo work without it; GitHub-hosted repos need: gh auth login --scopes \"repo,read:org\""
fi

# ── Step 2: bifrost.yaml ──────────────────────────────────────────

step "Step 2/10: Checking bifrost.yaml"

if [[ -f bifrost.yaml ]]; then
    ok "bifrost.yaml exists — leaving alone"
else
    cp bifrost.example.yaml bifrost.yaml
    ok "bifrost.yaml created from bifrost.example.yaml (edit it for your org)"
fi

# ── Step 3: uv sync ───────────────────────────────────────────────

step "Step 3/10: Installing Python dependencies (uv sync --all-extras)"
uv sync --all-extras
ok "Python deps in sync"

# ── Step 4: npm install ───────────────────────────────────────────

step "Step 4/10: Installing UI dependencies (npm install)"
pushd ui >/dev/null
if ! npm install 2>&1 | tee /tmp/bifrost-bootstrap-npm.log | tail -20; then
    fatal "See full log: /tmp/bifrost-bootstrap-npm.log"
fi
popd >/dev/null
ok "UI deps installed"

# ── Step 5: .env file ─────────────────────────────────────────────

step "Step 5/10: Configuring .env"

if [[ -f .env ]]; then
    ok ".env exists — leaving alone"
else
    # Resolve operator email from git config or prompt.
    OPERATOR_EMAIL=$(git config --get user.email || true)
    if [[ -z "$OPERATOR_EMAIL" ]]; then
        if [[ -t 0 ]]; then
            read -rp "  Operator email (used for BIFROST_OPERATOR_EMAIL): " OPERATOR_EMAIL
        else
            OPERATOR_EMAIL="dev@local"
            warn "No tty; defaulting BIFROST_OPERATOR_EMAIL=$OPERATOR_EMAIL — edit .env later."
        fi
    fi
    cat > .env <<EOF
# Generated by scripts/bootstrap.sh on $(date +%Y-%m-%d).
# Edit freely — pydantic-settings reads BIFROST_* on backend start.

# Postgres — matches docker-compose defaults.
BIFROST_DATABASE_URL=postgresql+asyncpg://bifrost:bifrost_dev@localhost:5432/bifrost

# Operator identity for local dev. Use this email directly.
BIFROST_OPERATOR_EMAIL=$OPERATOR_EMAIL

# Extraction throughput. 2 = safe. Bump if you have spare Claude rate-limit headroom.
BIFROST_MAX_CONCURRENT_EXTRACTIONS=2
EOF
    ok ".env created with BIFROST_OPERATOR_EMAIL=$OPERATOR_EMAIL"
fi

# ── Step 6: Postgres ──────────────────────────────────────────────

step "Step 6/10: Starting Postgres (docker compose up -d)"
docker compose up -d
ok "docker compose up issued"

# ── Step 7: Wait for healthy ──────────────────────────────────────

step "Step 7/10: Waiting for Postgres healthcheck"
for i in {1..30}; do
    status=$(docker inspect --format='{{.State.Health.Status}}' bifrost-postgres 2>/dev/null || echo "missing")
    if [[ "$status" = "healthy" ]]; then
        ok "Postgres healthy after ${i}s"
        break
    fi
    if [[ $i -eq 30 ]]; then
        docker logs bifrost-postgres --tail 30
        fatal "Postgres didn't reach 'healthy' in 30s. See logs above."
    fi
    sleep 1
done

# The test suite uses its own database; compose only creates `bifrost`.
PG_USER=$(docker exec bifrost-postgres printenv POSTGRES_USER)
PG_DB=$(docker exec bifrost-postgres printenv POSTGRES_DB)
if docker exec bifrost-postgres psql -U "$PG_USER" -d "$PG_DB" -tAc \
    "SELECT 1 FROM pg_database WHERE datname='bifrost_test'" | grep -q 1; then
    ok "bifrost_test database exists"
else
    docker exec bifrost-postgres createdb -U "$PG_USER" bifrost_test
    ok "bifrost_test database created"
fi

# ── Step 8: Migrations ────────────────────────────────────────────

step "Step 8/10: Applying database migrations (alembic upgrade head)"
uv run alembic upgrade head
ok "Migrations applied"

# ── Step 9: Start servers ─────────────────────────────────────────

step "Step 9/10: Starting backend + UI (make up)"
# `make up` is idempotent — restarts if already running.
make up
ok "Servers started"

# ── Step 10: Smoke ───────────────────────────────────────────────

step "Step 10/10: Smoke test"

# Wait briefly for the backend to bind.
for i in {1..15}; do
    if curl -sf -o /dev/null -w "%{http_code}" http://localhost:8000/api/v1/repos | grep -qE '^(200|401)$'; then
        ok "Backend responding on http://localhost:8000"
        break
    fi
    if [[ $i -eq 15 ]]; then
        warn "Backend didn't respond in 15s. Check: tail -f /tmp/bifrost-backend.log"
        break
    fi
    sleep 1
done

if curl -sf -o /dev/null http://localhost:5173; then
    ok "UI responding on http://localhost:5173"
else
    warn "UI not yet bound. Check: tail -f /tmp/bifrost-ui.log"
fi

# ── Done ─────────────────────────────────────────────────────────

echo ""
echo "${C_OK}${C_BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RESET}"
echo "${C_OK}${C_BOLD}✓ Bifrost bootstrap complete${C_RESET}"
echo "${C_OK}${C_BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RESET}"
echo ""
echo "  Open: ${C_BOLD}http://localhost:5173${C_RESET}"
echo ""
echo "  Next steps:"
echo "    1. Try the demo org: uv run python scripts/seed_acmeco.py --bifrost-cmd 'uv run bifrost' --register --extract"
echo "    2. Wait 3-15 min for extraction, then open 'Chat' and ask a question"
echo "    3. Or click 'Register repository' on Overview to index your own repo"
echo ""
echo "  Useful commands:"
echo "    make status       # who's alive?"
echo "    make logs         # tail both logs"
echo "    make restart      # restart everything"
echo "    make down         # stop everything (Postgres stays up)"
echo ""
echo "  Full guide:        RUNNING.md"
echo "  Troubleshooting:   RUNNING.md § Troubleshooting"
