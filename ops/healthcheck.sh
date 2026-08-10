#!/usr/bin/env bash
# Production health probe — exit non-zero on failure (for systemd/cron).
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
SETTINGS="${RMP_ROOT}/settings.json"
API_KEY="$(python3 -c "import json; print(json.load(open('${SETTINGS}'))['api_key'])" 2>/dev/null || echo '')"

FAIL=0

for unit in rmp-qdrant temporal-dev rmp-api rmp-worker openclaw-gateway; do
  if ! systemctl is-active --quiet "${unit}.service"; then
    echo "FAIL: ${unit} not active"
    FAIL=1
  fi
done

if [[ -n "${API_KEY}" ]]; then
  if ! curl -sf -H "X-RMP-API-Key: ${API_KEY}" http://127.0.0.1:8000/health >/dev/null; then
    echo "FAIL: RMP /health"
    FAIL=1
  fi
fi

"${RMP_ROOT}/venv/bin/python" -c "
import asyncio, json, sys
from app.production.readiness import run_all_checks
r = asyncio.run(run_all_checks())
if r.get('blocking_failures'):
    print('FAIL: readiness', r['blocking_failures'])
    sys.exit(1)
print('OK: readiness', r['summary'])
canary = next((c for c in r.get('checks', []) if c['name'] == 'memory_canary'), None)
stuck = next((c for c in r.get('checks', []) if c['name'] == 'stuck_workflows'), None)
if canary:
    print('memory_canary:', canary['status'], canary['message'])
if stuck:
    print('stuck_workflows:', stuck['status'], stuck['message'])
" 2>/dev/null || { echo "WARN: readiness check error"; FAIL=1; }

"${RMP_ROOT}/venv/bin/python" "${RMP_ROOT}/ops/llm_usage_report.py" 2>/dev/null \
  | "${RMP_ROOT}/venv/bin/python" -c "
import json, sys
d = json.load(sys.stdin)
tot = d.get('today_totals') or {}
print(f\"llm_usage: requests={tot.get('requests',0)} tokens={tot.get('total_tokens',0)} (today UTC)\")
for pid, counts in sorted((d.get('today_by_profile') or {}).items()):
    print(f\"  {pid}: req={counts.get('requests',0)} tok={counts.get('total_tokens',0)} rl={counts.get('rate_limits',0)}\")
" 2>/dev/null || true

exit "${FAIL}"
