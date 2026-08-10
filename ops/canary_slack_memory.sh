#!/usr/bin/env bash
# Live Slack memory canary: create task, verify completion, grep session for process memory usage.
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
SETTINGS="${RMP_ROOT}/settings.json"
API_KEY="${RMP_API_KEY:-$(python3 -c "import json; print(json.load(open('${SETTINGS}'))['api_key'])")}"
SESSION_DIR="${OPENCLAW_SESSIONS:-/root/.openclaw/agents/main/sessions}"
STAMP="$(date -u +%Y%m%dT%H%M%S)"
KEY="canary-memory:${STAMP}"

echo "=== RMP Slack memory canary (${STAMP}) ==="

RESP=$(curl -sf -X POST "http://127.0.0.1:8000/tasks" \
  -H "Content-Type: application/json" \
  -H "X-RMP-API-Key: ${API_KEY}" \
  -d "$(python3 -c "
import json
print(json.dumps({
  'intent': 'RMP MEMORY CANARY: Reply with one sentence summarizing what PROCESS-SCOPED MEMORY contains. No workspace memory_search.',
  'session_key': 'agent:main:main',
  'raw_text': 'RMP MEMORY CANARY test',
  'idempotency_key': '${KEY}',
  'tags': ['canary', 'memory-canary', 'force-canary-run'],
}))
")")

TASK_ID=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('task_id') or '')")
PROCESS_RUN=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('process_run_id') or '')")
SKIPPED=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('skipped', False))")
echo "Task: ${TASK_ID} process_run: ${PROCESS_RUN}"

if [[ -z "$TASK_ID" ]]; then
  echo "CANARY FAIL: no task_id from POST (skipped=${SKIPPED})"
  echo "$RESP"
  exit 1
fi

STATUS="running"
for i in $(seq 1 72); do
  sleep 10
  STATUS=$(curl -sf -H "X-RMP-API-Key: ${API_KEY}" "http://127.0.0.1:8000/tasks/${TASK_ID}" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))")
  echo "  poll ${i}: ${STATUS}"
  if [[ "$STATUS" == "completed" ]]; then
    break
  fi
  if [[ "$STATUS" == "failed" || "$STATUS" == "compensated" ]]; then
    break
  fi
done

RMP_SESSION="agent:main:rmp_task_${TASK_ID}"
SESSION_FILE=""
SESSION_FILE=$(python3 -c "
import json, os, sys
task_id = sys.argv[1]
session_dir = sys.argv[2]
index_path = os.path.join(session_dir, 'sessions.json')
session_key = f'agent:main:rmp_task_{task_id}'
try:
    data = json.load(open(index_path))
    entry = data.get(session_key) or {}
    sid = entry.get('sessionId') or entry.get('id') or ''
    if sid:
        print(os.path.join(session_dir, f'{sid}.jsonl'))
except Exception:
    pass
" "$TASK_ID" "$SESSION_DIR" 2>/dev/null || true)

MEMORY_OK=0
SEARCH_BAD=0
PROMPT_OK=0
if [[ -n "$SESSION_FILE" && -f "$SESSION_FILE" ]]; then
  echo "Session transcript: ${SESSION_FILE}"
  if grep -qi "PROCESS-SCOPED MEMORY" "$SESSION_FILE"; then
    MEMORY_OK=1
    echo "PASS: PROCESS-SCOPED MEMORY found in session"
  else
    echo "WARN: PROCESS-SCOPED MEMORY not found in session"
  fi
  if grep -qi "do not use memory_search\|Do NOT use memory_search" "$SESSION_FILE"; then
    PROMPT_OK=1
    echo "PASS: memory-first instruction present in dispatch prompt"
  fi
  if grep -qi 'memory_search' "$SESSION_FILE" && [[ "$PROMPT_OK" -eq 0 ]]; then
    SEARCH_BAD=1
    echo "FAIL: workspace memory_search used without memory-first guard"
  else
    echo "PASS: no unconstrained workspace memory_search detected"
  fi
else
  echo "WARN: rmp_task session transcript not found at ${SESSION_FILE:-unknown}"
fi

CTX=$(curl -sf -H "X-RMP-API-Key: ${API_KEY}" "http://127.0.0.1:8000/memory/process/${PROCESS_RUN}/context" 2>/dev/null || echo '{}')
echo "Memory context API count: $(echo "$CTX" | python3 -c "import json,sys; print(json.load(sys.stdin).get('count',0))" 2>/dev/null || echo 0)"

if [[ "$STATUS" != "completed" ]]; then
  echo "CANARY FAIL: task status=${STATUS}"
  RESULT_FILE="${RMP_ROOT}/data/last_memory_canary.json"
  mkdir -p "$(dirname "${RESULT_FILE}")"
  final_status="${STATUS}"
  if [[ "$STATUS" == "running" || "$STATUS" == "created" ]]; then
    final_status="timeout"
  fi
  python3 -c "
import json, datetime
print(json.dumps({
  'status': '${final_status}',
  'task_id': '${TASK_ID}',
  'process_run_id': '${PROCESS_RUN}',
  'memory_ok': ${MEMORY_OK},
  'prompt_ok': ${PROMPT_OK},
  'search_bad': ${SEARCH_BAD},
  'finished_at': datetime.datetime.utcnow().isoformat() + 'Z',
}))
" > "${RESULT_FILE}"
  if [[ "$MEMORY_OK" -eq 1 && "$PROMPT_OK" -eq 1 && "$SEARCH_BAD" -eq 0 ]]; then
    echo "NOTE: memory transcript checks passed; task did not reach completed (likely workflow timeout/stuck)"
  fi
  (
    cd "${RMP_ROOT}"
    ./venv/bin/python -m app.production.canary_sentinel --trigger memory_canary
  ) || true
  exit 1
fi

if [[ "$SEARCH_BAD" -eq 1 ]]; then
  echo "CANARY FAIL: workspace memory_search dominance"
  (
    cd "${RMP_ROOT}"
    ./venv/bin/python -m app.production.canary_sentinel --trigger memory_canary
  ) || true
  exit 1
fi

echo "CANARY OK (status=${STATUS}, memory_ok=${MEMORY_OK}, prompt_ok=${PROMPT_OK})"

RESULT_FILE="${RMP_ROOT}/data/last_memory_canary.json"
mkdir -p "$(dirname "${RESULT_FILE}")"
python3 -c "
import json, datetime
print(json.dumps({
  'status': 'completed' if '${STATUS}' == 'completed' else '${STATUS}',
  'task_id': '${TASK_ID}',
  'process_run_id': '${PROCESS_RUN}',
  'memory_ok': ${MEMORY_OK},
  'prompt_ok': ${PROMPT_OK},
  'search_bad': ${SEARCH_BAD},
  'session_file': '${SESSION_FILE}',
  'finished_at': datetime.datetime.utcnow().isoformat() + 'Z',
}))
" > "${RESULT_FILE}"
echo "Wrote ${RESULT_FILE}"
exit 0
