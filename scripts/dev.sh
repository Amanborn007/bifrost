#!/usr/bin/env bash
# Bifrost dev-server helper.
#
# Solves the recurring "nohup got suspended" / "zsh wrapped my
# command" / "setsid doesn't exist on macOS" pain by centralizing
# start/stop/restart/status/logs/health into one command that works
# reliably on macOS + zsh.
#
# Usage:
#   ./scripts/dev.sh up         # start backend + ui
#   ./scripts/dev.sh down       # stop both
#   ./scripts/dev.sh restart    # stop + start (what you want 99% of the time)
#   ./scripts/dev.sh backend    # restart backend only
#   ./scripts/dev.sh ui         # restart ui only
#   ./scripts/dev.sh status     # show what's running
#   ./scripts/dev.sh logs       # tail both logs
#   ./scripts/dev.sh logs backend
#   ./scripts/dev.sh logs ui
#   ./scripts/dev.sh health     # hit both health endpoints
#
# Or via the Makefile at repo root: `make restart`, `make status`, etc.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_LOG=/tmp/bifrost-backend.log
UI_LOG=/tmp/bifrost-ui.log
BACKEND_PID=/tmp/bifrost-backend.pid
UI_PID=/tmp/bifrost-ui.pid
BACKEND_PORT=8000
UI_PORT=5173

# ANSI colors (only when output is a tty)
if [[ -t 1 ]]; then
    c_ok=$'\033[32m'; c_warn=$'\033[33m'; c_err=$'\033[31m'
    c_dim=$'\033[90m'; c_bold=$'\033[1m'; c_reset=$'\033[0m'
else
    c_ok= c_warn= c_err= c_dim= c_bold= c_reset=
fi

ok()   { echo "${c_ok}✓${c_reset} $*"; }
warn() { echo "${c_warn}!${c_reset} $*"; }
err()  { echo "${c_err}✗${c_reset} $*" >&2; }
dim()  { echo "${c_dim}$*${c_reset}"; }

is_running() {
    local pid_file="$1"
    [[ -f "$pid_file" ]] || return 1
    local pid; pid=$(cat "$pid_file" 2>/dev/null || true)
    [[ -n "$pid" ]] || return 1
    kill -0 "$pid" 2>/dev/null
}

port_owner() {
    lsof -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1
}

wait_for_health() {
    # tries default 60 (×0.5s = 30s) — vite's first-run dep re-optimize
    # can exceed the old 20s budget. Callers may override via $3.
    local url="$1" label="$2" tries="${3:-60}" status
    while [[ $tries -gt 0 ]]; do
        status=$(curl -sS -o /dev/null -w '%{http_code}' -m 2 "$url" 2>/dev/null || echo "000")
        if [[ "$status" =~ ^[23] ]]; then
            ok "$label is up (HTTP $status)"
            return 0
        fi
        sleep 0.5
        ((tries--))
    done
    err "$label failed health check after 20s (last status=$status)"
    return 1
}

# The reliable macOS backgrounding pattern: run in a subshell that
# exits immediately. The grandchild gets reparented to init (launchd)
# and survives the parent shell closing. No nohup + disown needed.
spawn_detached() {
    local cwd="$1" log="$2" pid_file="$3"; shift 3
    (
        cd "$cwd"
        # Replace subshell with the real process so pid_file points at it.
        exec "$@" > "$log" 2>&1 < /dev/null
    ) &
    local pid=$!
    echo "$pid" > "$pid_file"
    # Detach from THIS shell's job table too (belt+suspenders).
    disown 2>/dev/null || true
}

kill_tree() {
    local pid="$1"
    [[ -n "${pid:-}" ]] || return 0
    # Kill the process group so `uv run` + its uvicorn child both go.
    kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    sleep 0.5
    kill -KILL -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
}

start_backend() {
    if is_running "$BACKEND_PID"; then
        warn "backend already running (pid $(cat $BACKEND_PID))"
        return 0
    fi
    local stale; stale=$(port_owner "$BACKEND_PORT" || true)
    if [[ -n "$stale" ]]; then
        warn "port $BACKEND_PORT occupied by pid $stale — killing"
        kill -9 "$stale" 2>/dev/null || true
        sleep 0.5
    fi
    echo "starting backend on :$BACKEND_PORT (log: $BACKEND_LOG)"
    spawn_detached "$REPO_DIR" "$BACKEND_LOG" "$BACKEND_PID" \
        uv run uvicorn bifrost.api.app:create_app \
            --factory --host 127.0.0.1 --port "$BACKEND_PORT"
    dim "  pid $(cat $BACKEND_PID)"
    wait_for_health "http://127.0.0.1:$BACKEND_PORT/api/v1/repos" backend || {
        err "startup failed — last 30 lines of log:"
        tail -n 30 "$BACKEND_LOG" || true
        return 1
    }
}

start_ui() {
    if is_running "$UI_PID"; then
        warn "ui already running (pid $(cat $UI_PID))"
        return 0
    fi
    local stale; stale=$(port_owner "$UI_PORT" || true)
    if [[ -n "$stale" ]]; then
        warn "port $UI_PORT occupied by pid $stale — killing"
        kill -9 "$stale" 2>/dev/null || true
        sleep 0.5
    fi
    echo "starting ui on :$UI_PORT (log: $UI_LOG)"
    # Bind vite to 127.0.0.1 explicitly: vite's default "localhost" can
    # resolve to IPv6 ::1 only on macOS, which made the 127.0.0.1 health
    # probe below false-negative — and the resulting non-zero `make up`
    # exit then tore down the (just-started) vite process. Pinning the
    # host makes the probe match, so startup reports success and vite
    # survives, exactly like the backend.
    spawn_detached "$REPO_DIR/ui" "$UI_LOG" "$UI_PID" \
        npm run dev -- --port "$UI_PORT" --strictPort --host 127.0.0.1
    dim "  pid $(cat $UI_PID)"
    wait_for_health "http://127.0.0.1:$UI_PORT/" ui || {
        err "ui startup failed — last 30 lines of log:"
        tail -n 30 "$UI_LOG" || true
        return 1
    }
}

stop_backend() {
    if is_running "$BACKEND_PID"; then
        local pid; pid=$(cat "$BACKEND_PID")
        echo "stopping backend (pid $pid)…"
        kill_tree "$pid"
    fi
    # Sweep stragglers matching the exact cmdline
    pkill -9 -f "uvicorn bifrost.api.app" 2>/dev/null || true
    rm -f "$BACKEND_PID"
    ok "backend stopped"
}

stop_ui() {
    if is_running "$UI_PID"; then
        local pid; pid=$(cat "$UI_PID")
        echo "stopping ui (pid $pid)…"
        kill_tree "$pid"
    fi
    # Kill any vite bound to our UI_PORT (scoped — don't touch other
    # projects' vite servers on different ports).
    local stale; stale=$(port_owner "$UI_PORT" || true)
    if [[ -n "$stale" ]]; then
        kill -9 "$stale" 2>/dev/null || true
    fi
    rm -f "$UI_PID"
    ok "ui stopped"
}

cmd_status() {
    printf "${c_bold}%-9s %-8s %-6s %s${c_reset}\n" SERVICE PID PORT STATUS
    _row backend "$BACKEND_PID" "$BACKEND_PORT"
    _row ui      "$UI_PID"      "$UI_PORT"
}
_row() {
    local name="$1" pid_file="$2" port="$3" pid status owner
    if is_running "$pid_file"; then
        pid=$(cat "$pid_file")
        status="${c_ok}UP${c_reset}"
    else
        owner=$(port_owner "$port" || true)
        if [[ -n "$owner" ]]; then
            pid="$owner"
            status="${c_warn}UP (not ours)${c_reset}"
        else
            pid="-"
            status="${c_dim}DOWN${c_reset}"
        fi
    fi
    printf "%-9s %-8s %-6s %b\n" "$name" "$pid" "$port" "$status"
}

cmd_logs() {
    local which="${1:-all}"
    case "$which" in
        backend) tail -n 200 -f "$BACKEND_LOG" ;;
        ui)      tail -n 200 -f "$UI_LOG" ;;
        all|*)   tail -n 100 -f "$BACKEND_LOG" "$UI_LOG" ;;
    esac
}

cmd_health() {
    printf "backend  "
    curl -sS -o /dev/null -w 'status=%{http_code} time=%{time_total}s\n' \
        -m 5 "http://127.0.0.1:$BACKEND_PORT/api/v1/repos" \
        || echo "status=CONN_REFUSED"
    printf "ui       "
    curl -sS -o /dev/null -w 'status=%{http_code} time=%{time_total}s\n' \
        -m 5 "http://127.0.0.1:$UI_PORT/" \
        || echo "status=CONN_REFUSED"
}

cmd_help() {
    cat <<EOF
${c_bold}Bifrost dev helper${c_reset}

${c_bold}Commands${c_reset}
  up          start backend + ui
  down        stop both
  restart     down + up (most common)
  backend     restart backend only (picks up python changes)
  ui          restart ui only
  status      show pid + port + status
  logs [backend|ui|all]   tail logs (default: all)
  health      hit /api/v1/repos and /

${c_bold}Make-only targets${c_reset}
  make migrate   uv run alembic upgrade head
  make test      run the test suite (needs Postgres)

${c_bold}Files${c_reset}
  backend log  $BACKEND_LOG
  ui log       $UI_LOG
  backend pid  $BACKEND_PID
  ui pid       $UI_PID

${c_bold}Tips${c_reset}
  Wrapper: ${c_dim}make restart${c_reset} (or any subcommand)
  Quick log watch: ${c_dim}./scripts/dev.sh logs backend${c_reset}
  Alias for zsh:  ${c_dim}alias bd='/path/to/bifrost/scripts/dev.sh'${c_reset}
EOF
}

cmd="${1:-help}"
shift || true
case "$cmd" in
    up|start)      start_backend; start_ui ;;
    down|stop)     stop_backend; stop_ui ;;
    restart)       stop_backend; stop_ui; start_backend; start_ui ;;
    backend)       stop_backend; start_backend ;;
    ui)            stop_ui; start_ui ;;
    status)        cmd_status ;;
    logs)          cmd_logs "${1:-}" ;;
    health)        cmd_health ;;
    help|--help|-h|*) cmd_help ;;
esac
