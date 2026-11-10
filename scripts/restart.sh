#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   scripts/restart.sh              # restart API only
#   scripts/restart.sh --tail        # restart + tail the log
#   scripts/restart.sh --ui          # restart API + UI dev server
#   scripts/restart.sh --ui --tail   # both

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export REPO_ROOT

TAIL_AFTER=0
RESTART_UI=0
for arg in "$@"; do
  case "$arg" in
    --tail|-t) TAIL_AFTER=1 ;;
    --ui|-u)   RESTART_UI=1 ;;
    --help|-h)
      echo "Usage: scripts/restart.sh [--tail] [--ui]"
      echo "  --tail   tail /tmp/bifrost_server.log after start"
      echo "  --ui     also restart vite dev server on :5173"
      exit 0 ;;
    *) echo "unknown arg: $arg (try --help)"; exit 1 ;;
  esac
done

# Kill anything listening on :8000 + any uvicorn stragglers.
echo "→ stopping API on :8000"
PIDS=$(lsof -ti :8000 -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$PIDS" ]; then kill $PIDS 2>/dev/null || true; sleep 2; fi
pkill -f "uvicorn bifrost.api.app" 2>/dev/null || true
sleep 1

if [ "$RESTART_UI" = "1" ]; then
  echo "→ stopping UI dev server on :5173"
  UI_PIDS=$(lsof -ti :5173 -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "$UI_PIDS" ]; then kill $UI_PIDS 2>/dev/null || true; sleep 1; fi
  pkill -f "vite --port 5173" 2>/dev/null || true
fi

# Clean stale env vars — force .env values through
unset BIFROST_EXTRACTION_DRY_RUN

cd "$REPO_ROOT"

# Fully detach from tty (macOS has no setsid). Use a double-fork via python
# so the uvicorn process won't inherit a tty and child `claude auth status`
# subprocess won't hang on tty input.
python3 - <<'PYEOF' &
import os, sys
# Double-fork to detach from tty
if os.fork() > 0: sys.exit(0)
os.setsid()
if os.fork() > 0: sys.exit(0)
# In the grandchild: redirect stdin/out/err and exec uvicorn
with open("/dev/null") as devnull:
    os.dup2(devnull.fileno(), 0)
with open("/tmp/bifrost_server.log", "ab") as logf:
    os.dup2(logf.fileno(), 1)
    os.dup2(logf.fileno(), 2)
os.chdir(os.environ["REPO_ROOT"])
os.execvp("uv", ["uv", "run", "uvicorn",
                  "bifrost.api.app:create_app", "--factory", "--port", "8000"])
PYEOF
wait  # let the outer fork finish (it exits immediately)

# Wait for listener (up to 30s)
for i in $(seq 1 30); do
  if lsof -i :8000 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "up after ${i}s"
    break
  fi
  sleep 1
  if [ "$i" = "30" ]; then
    echo "FAILED — check /tmp/bifrost_server.log"
    tail -20 /tmp/bifrost_server.log
    exit 1
  fi
done

echo ""
echo "--- health ---"
curl -s http://localhost:8000/api/v1/health/claude-cli | python3 -m json.tool 2>/dev/null | head -10
echo ""
echo "--- env sanity (should say dry_run=False) ---"
grep -E "dry_run|worker_started" /tmp/bifrost_server.log | tail -3

if [ "$RESTART_UI" = "1" ]; then
  echo ""
  echo "→ starting UI dev server on :5173"
  cd "$REPO_ROOT/ui"
  nohup npm run dev > /tmp/bifrost_ui.log 2>&1 &
  cd "$REPO_ROOT"
  for i in $(seq 1 20); do
    if lsof -i :5173 -sTCP:LISTEN >/dev/null 2>&1; then
      echo "UI up after ${i}s on http://localhost:5173"
      break
    fi
    sleep 1
  done
fi

if [ "$TAIL_AFTER" = "1" ]; then
  echo ""
  echo "--- tailing /tmp/bifrost_server.log (Ctrl-C to stop) ---"
  tail -F /tmp/bifrost_server.log
fi
