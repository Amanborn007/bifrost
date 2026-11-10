#!/usr/bin/env bash
# scripts/pr-stability-check.sh — validate that the committed state is
# actually running / testable. Run before opening any PR.
#
# Convention: every Bifrost PR ships code the author has locally
# validated against HEAD. This script encodes that contract as 8 gates.
# Any failure is a ✗; any ✗ means the PR description should explain it.
#
# Exit codes:
#   0  — all 8 gates pass
#   1  — one or more ✗ — don't open the PR yet
#
# Usage:
#   ./scripts/pr-stability-check.sh
#   ./scripts/pr-stability-check.sh --skip-ui     # when only backend changed
#   ./scripts/pr-stability-check.sh --json        # machine-readable
#
# Design notes:
#   * Runs against the *currently running* backend (port 8000) + ui
#     (port 5173). If services aren't up, starts them via ./scripts/dev.sh.
#   * Does NOT modify the tree or DB. Read-only checks + one test run.
#   * Includes a shadow-import audit because that class of bug
#     (``UnboundLocalError``) crashes the research pipeline at runtime and
#     is only reliably caught via AST walk.

set -u
cd "$(dirname "$0")/.."

SKIP_UI=0
JSON=0
for arg in "$@"; do
  case "$arg" in
    --skip-ui) SKIP_UI=1 ;;
    --json)    JSON=1 ;;
  esac
done

# ── Output helpers (TTY-aware) ────────────────────────────────
if [ -t 1 ] && [ "$JSON" -eq 0 ]; then
  C_PASS=$'\033[32m' C_FAIL=$'\033[31m' C_DIM=$'\033[2m' C_BOLD=$'\033[1m' C_RST=$'\033[0m'
else
  C_PASS= C_FAIL= C_DIM= C_BOLD= C_RST=
fi

declare -a RESULTS
record() {
  # record <id> <label> <status> <detail>
  RESULTS+=("$1|$2|$3|$4")
}

fmt() {
  if [ "$JSON" -eq 1 ]; then
    printf '['
    local first=1
    for r in "${RESULTS[@]}"; do
      IFS='|' read -r id label status detail <<< "$r"
      [ $first -eq 0 ] && printf ','
      first=0
      printf '{"id":"%s","label":"%s","status":"%s","detail":"%s"}' \
        "$id" "$label" "$status" "$(printf '%s' "$detail" | sed 's/"/\\"/g')"
    done
    printf ']\n'
  else
    echo
    echo "${C_BOLD}── PR stability check ─────────────────────────────${C_RST}"
    for r in "${RESULTS[@]}"; do
      IFS='|' read -r id label status detail <<< "$r"
      if [ "$status" = "pass" ]; then
        printf '  %s✓%s %-22s %s%s%s\n' "$C_PASS" "$C_RST" "$label" "$C_DIM" "$detail" "$C_RST"
      elif [ "$status" = "skip" ]; then
        printf '  %s·%s %-22s %s%s%s\n' "$C_DIM" "$C_RST" "$label" "$C_DIM" "$detail" "$C_RST"
      else
        printf '  %s✗%s %-22s %s\n' "$C_FAIL" "$C_RST" "$label" "$detail"
      fi
    done
  fi
}

# ── 1. Working tree clean ────────────────────────────────────
dirty=$(git status --porcelain | wc -l | tr -d ' ')
if [ "$dirty" -eq 0 ]; then
  record working-tree "Working tree" pass "clean"
else
  record working-tree "Working tree" fail "$dirty uncommitted change(s) — committed state ≠ running state"
fi

# ── 2. HEAD is pushed to origin ──────────────────────────────
current_branch=$(git rev-parse --abbrev-ref HEAD)
local_head=$(git rev-parse HEAD)
if git rev-parse --verify "origin/${current_branch}" >/dev/null 2>&1; then
  remote_head=$(git rev-parse "origin/${current_branch}")
  if [ "$local_head" = "$remote_head" ]; then
    record head-pushed "HEAD pushed" pass "$current_branch @ ${local_head:0:8}"
  else
    record head-pushed "HEAD pushed" fail "local ${local_head:0:8} ≠ origin ${remote_head:0:8}"
  fi
else
  record head-pushed "HEAD pushed" fail "branch $current_branch not on origin yet"
fi

# ── 3. UI typecheck ──────────────────────────────────────────
if [ "$SKIP_UI" -eq 1 ]; then
  record ui-tsc "UI typecheck" skip "--skip-ui"
else
  if ( cd ui && npx -y tsc --noEmit ) >/tmp/bifrost-stability-tsc.log 2>&1; then
    record ui-tsc "UI typecheck" pass "clean"
  else
    errs=$(grep -c "error TS" /tmp/bifrost-stability-tsc.log 2>/dev/null || echo 0)
    record ui-tsc "UI typecheck" fail "$errs errors (see /tmp/bifrost-stability-tsc.log)"
  fi
fi

# ── 4. UI vitest ─────────────────────────────────────────────
if [ "$SKIP_UI" -eq 1 ]; then
  record ui-tests "UI tests" skip "--skip-ui"
else
  if ( cd ui && npx -y vitest run ) >/tmp/bifrost-stability-vitest.log 2>&1; then
    total=$(grep -oE 'Tests  [0-9]+ passed \([0-9]+\)' /tmp/bifrost-stability-vitest.log | tail -1)
    record ui-tests "UI tests" pass "$total"
  else
    record ui-tests "UI tests" fail "vitest failed (see /tmp/bifrost-stability-vitest.log)"
  fi
fi

# ── 5. Python imports (catches shadow-import traps at load) ─
import_probe=$(uv run python -c "
import importlib, pkgutil, sys
failed = []
import bifrost
for mod in pkgutil.walk_packages(bifrost.__path__, prefix='bifrost.'):
    try:
        importlib.import_module(mod.name)
    except Exception as e:
        failed.append((mod.name, type(e).__name__ + ': ' + str(e)[:80]))
if failed:
    for n, e in failed[:5]:
        print(n, '→', e)
    sys.exit(1)
print('OK')
" 2>&1)
if [ $? -eq 0 ] && [ "$import_probe" = "OK" ]; then
  record py-imports "Python imports" pass "every bifrost.* module loads"
else
  first=$(echo "$import_probe" | head -1 | cut -c 1-90)
  record py-imports "Python imports" fail "$first"
fi

# ── 6. Shadow-import audit ──────────────────────────────────
shadow=$(uv run python - <<'PY' 2>/dev/null
import ast, pathlib, sys
total = 0
for p in pathlib.Path('src/bifrost').rglob('*.py'):
    try: tree = ast.parse(p.read_text())
    except SyntaxError: continue
    mod = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names: mod.add(a.asname or a.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names: mod.add(a.asname or a.name)
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for s in ast.walk(fn):
                if isinstance(s, (ast.Import, ast.ImportFrom)) and s is not fn:
                    for a in s.names:
                        n = a.asname or (a.name if isinstance(s, ast.ImportFrom) else a.name.split('.')[0])
                        if n in mod:
                            total += 1
print(total)
PY
)
if [ "${shadow:-0}" = "0" ]; then
  record shadow-imports "Shadow imports" pass "0 found"
else
  record shadow-imports "Shadow imports" fail "$shadow found (run the AST audit in scripts/)"
fi

# ── 7. Alembic head matches source ──────────────────────────
alembic_src_head=$(ls alembic/versions/ | grep -oE '^[0-9]+' | sort -n | tail -1)
alembic_db_head=$(uv run alembic current 2>/dev/null | grep -oE '[0-9]{3}' | head -1)
if [ -z "$alembic_db_head" ]; then
  record alembic "Alembic" fail "no revision on DB — run alembic upgrade head"
elif [ "$alembic_src_head" = "$alembic_db_head" ]; then
  record alembic "Alembic" pass "head @ $alembic_db_head"
else
  record alembic "Alembic" fail "src @ $alembic_src_head, db @ $alembic_db_head — migrate"
fi

# ── 8. Live services respond ────────────────────────────────
backend_code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/health || echo 000)
ui_code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:5173 || echo 000)
if [ "$backend_code" = "200" ] && { [ "$ui_code" = "200" ] || [ "$SKIP_UI" -eq 1 ]; }; then
  record services "Services up" pass "backend 200, ui $ui_code"
elif [ "$backend_code" = "200" ]; then
  record services "Services up" fail "backend 200 but ui $ui_code — is vite running?"
else
  record services "Services up" fail "backend $backend_code — did ./scripts/dev.sh up fire?"
fi

# ── Render + exit ────────────────────────────────────────────
fmt
fails=$(printf '%s\n' "${RESULTS[@]}" | awk -F'|' '$3=="fail"' | wc -l | tr -d ' ')
if [ "$fails" -gt 0 ]; then
  if [ "$JSON" -eq 0 ]; then
    echo
    echo "${C_FAIL}${C_BOLD}${fails} gate(s) failed — don't open the PR yet.${C_RST}"
  fi
  exit 1
fi
if [ "$JSON" -eq 0 ]; then
  echo
  echo "${C_PASS}${C_BOLD}All gates passed — PR is stable.${C_RST}"
fi
exit 0
