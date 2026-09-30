#!/usr/bin/env bash
# Production health probe — exit non-zero on failure (for systemd/cron).
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
SETTINGS="${RMP_ROOT}/settings.json"
API_KEY="$(python3 -c "import json; print(json.load(open('${SETTINGS}'))['api_key'])" 2>/dev/null || echo '')"

FAIL=0

for unit in rmp-qdrant temporal rmp-api rmp-worker openclaw-gateway; do
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
deep = ('deep_memory_ingest', 'deep_memory_enrichment', 'deep_memory_index', 'memory_lane', 'deep_recall',
        'task_documents', 'deep_index_internal', 'judged_followups')
for c in r.get('checks', []):
    if c['name'] in deep:
        print(c['name'] + ':', c['status'], c['message'])
" 2>/dev/null || { echo "WARN: readiness check error"; FAIL=1; }

"${RMP_ROOT}/venv/bin/python" "${RMP_ROOT}/ops/llm_usage_report.py" 2>/dev/null \
  | "${RMP_ROOT}/venv/bin/python" -c "
import json, sys
d = json.load(sys.stdin)
tot = d.get('today_totals') or {}
print(f\"llm_usage: requests={tot.get('requests',0)} tokens={tot.get('total_tokens',0)} (today UTC)\")
for pid, counts in sorted((d.get('today_by_profile') or {}).items()):
    print(f\"  {pid}: req={counts.get('requests',0)} tok={counts.get('total_tokens',0)} rl={counts.get('rate_limits',0)}\")
note = d.get('unattributed_note')
if note:
    print('  unattributed:', note)
    for day in d.get('unattributed_historical_days') or []:
        print(f\"  unattributed_day: {day}\")
tr = d.get('transcripts_24h') or {}
if tr.get('available'):
    t = tr['totals']
    ctx = tr.get('max_live_context') or {}
    print(f\"llm_transcripts_24h: prompt={t['input_tokens'] + t['cache_read_tokens']:,} aborted_prompt={t['aborted_prompt_tokens']:,} output={t['output_tokens']:,} attempts={t['attempts']} aborted={t['aborted']} ({tr.get('abort_rate', 0):.0%}) max_live_context={ctx.get('tokens', 0):,} ({ctx.get('session_key') or '-'})\")
    for cat, c in sorted((tr.get('by_category') or {}).items()):
        print(f\"  {cat}: attempts={c['attempts']} aborted={c['aborted']} prompt={c['input_tokens'] + c['cache_read_tokens']:,} aborted_prompt={c['aborted_prompt_tokens']:,}\")
for alert in d.get('transcript_alerts') or []:
    print(f'WARN: llm_usage {alert}')
" 2>/dev/null || true

exit "${FAIL}"
