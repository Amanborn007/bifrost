#!/usr/bin/env bash
# One snapshot of extraction health. Appends to /tmp/bifrost_findings.log
# so we can review everything at the end.

BASE="${BIFROST_API:-http://localhost:8000/api/v1}"
TS=$(date +"%H:%M:%S")
FINDINGS=/tmp/bifrost_findings.log

{
echo ""
echo "======================================================================"
echo "== SNAPSHOT $TS =="
echo "======================================================================"

echo ""
echo "-- Task state --"
curl -s "$BASE/tasks?limit=50" 2>/dev/null | python3 -c "
import json, sys
from collections import Counter
d = [t for t in json.load(sys.stdin).get('data', []) if t.get('type')=='extraction']
c = Counter(t['status'] for t in d)
print('by status:', dict(c))
# any failed?
failed = [t for t in d if t['status']=='failed']
if failed:
    print(f'FAILED TASKS ({len(failed)}):')
    for t in failed:
        err = (t.get('error_message') or '')[:200]
        print(f\"  {t['query'][:40]} — {err}\")
# any delivered?
delivered = [t for t in d if t['status']=='delivered']
if delivered:
    print(f'DELIVERED ({len(delivered)}):')
    for t in delivered:
        r = t.get('result') or {}
        print(f\"  {t['query'][:40]:<40} comp={r.get('components',0)} flow={r.get('flows',0)} dep={r.get('dependencies',0)} rel={r.get('relationships',0)} idx={r.get('knowledge_index',0)}\")"

echo ""
echo "-- Cost total --"
curl -s "$BASE/costs/summary?window=all" 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin).get('data', {})
print(f\"  total: \${d.get('total_cost_usd',0):.2f}  calls: {d.get('total_calls',0)}\")
print('  by phase:')
for row in d.get('by_phase', []):
    print(f\"    {row.get('label', row['key']):<15} \${row.get('cost_usd',0):.2f}  ({row.get('calls',0)} calls)\")
"

echo ""
echo "-- Phase E behavior (our recent fix) --"
SKIPS=$(grep -cE "Phase E: skipping" /tmp/bifrost_server.log 2>/dev/null || echo 0)
HANGS=$(grep -cE "Phase E retry|Phase E failed after 3" /tmp/bifrost_server.log 2>/dev/null || echo 0)
echo "  enum/POJO skipped: $SKIPS"
echo "  Phase E hangs/failures: $HANGS (should be 0)"

echo ""
echo "-- Any NEW errors in the log (since last snapshot) --"
# Get log lines since last snapshot marker
LAST_SNAPSHOT=$(grep -n "== SNAPSHOT" $FINDINGS 2>/dev/null | tail -1 | cut -d: -f1)
grep -iE "Traceback|ERROR|IntegrityError|AssertionError|Canary echoed|failed after" /tmp/bifrost_server.log 2>&1 \
  | grep -v "http.request" \
  | tail -10

echo ""
echo "-- Entity counts per repo in domain ${DOMAIN_NAME:-dlvr} --"
docker exec -i $(docker ps --filter name=bifrost-postgres -q | head -1) psql -U bifrost -d bifrost -t -c "
SELECT r.name, e.entity_type, COUNT(*)
FROM repos r LEFT JOIN entities e ON r.id=e.scope_repo_id
WHERE r.domain_id=(SELECT id FROM domains WHERE name='${DOMAIN_NAME:-dlvr}')
GROUP BY r.name, e.entity_type
ORDER BY r.name, e.entity_type NULLS FIRST;" 2>&1 | head -30

} | tee -a $FINDINGS
