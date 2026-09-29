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

ACTIVE_USERS=$(
  cd "${RMP_ROOT}" && ./venv/bin/python -c "
from app.production.canary_sentinel import count_active_user_tasks_sync
print(count_active_user_tasks_sync())
" 2>/dev/null || echo 0
)
if [[ "${ACTIVE_USERS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "CANARY SKIP: ${ACTIVE_USERS} active user task(s) — deferring memory canary"
  cd "${RMP_ROOT}" && ./venv/bin/python -c "
from app.production.canary_sentinel import maybe_mark_memory_canary_deferred
maybe_mark_memory_canary_deferred(reason='active_user_tasks', active_users=int('${ACTIVE_USERS}'))
" || true
  exit 0
fi
USER_SLOTS=$(
  cd "${RMP_ROOT}" && ./venv/bin/python -c "
from app.llm.quota_broker import _count_kind_slots, _read_state
print(_count_kind_slots(_read_state())[0])
" 2>/dev/null || echo 0
)
if [[ "${USER_SLOTS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "CANARY SKIP: ${USER_SLOTS} user LLM slot(s) in use — deferring memory canary"
  cd "${RMP_ROOT}" && ./venv/bin/python -c "
from app.production.canary_sentinel import maybe_mark_memory_canary_deferred
maybe_mark_memory_canary_deferred(reason='user_llm_slots', active_users=int('${USER_SLOTS}'))
" || true
  exit 0
fi

# Hourly health canary starts at :07 and holds the single canary slot for ~30s.
# Wait for that slot instead of creating a task that compensates.
for _ in $(seq 1 12); do
  SLOT_COUNTS=$(
    cd "${RMP_ROOT}" && ./venv/bin/python -c "
from app.llm.quota_broker import _count_kind_slots, _read_state
print('%s %s' % _count_kind_slots(_read_state()))
" 2>/dev/null || echo "0 0"
  )
  USER_NOW="${SLOT_COUNTS%% *}"
  CANARY_NOW="${SLOT_COUNTS##* }"
  if [[ "${USER_NOW}" =~ ^[1-9][0-9]*$ ]]; then
    echo "CANARY SKIP: ${USER_NOW} user LLM slot(s) in use — deferring memory canary"
    cd "${RMP_ROOT}" && ./venv/bin/python -c "
from app.production.canary_sentinel import maybe_mark_memory_canary_deferred
maybe_mark_memory_canary_deferred(reason='user_llm_slots', active_users=int('${USER_NOW}'))
" || true
    exit 0
  fi
  if [[ ! "${CANARY_NOW}" =~ ^[1-9][0-9]*$ ]]; then
    break
  fi
  echo "CANARY WAIT: ${CANARY_NOW} canary LLM slot(s) in use"
  sleep 5
done
if [[ "${CANARY_NOW:-0}" =~ ^[1-9][0-9]*$ ]]; then
  echo "CANARY SKIP: canary LLM slot still busy — deferring memory canary"
  cd "${RMP_ROOT}" && ./venv/bin/python -c "
from app.production.canary_sentinel import maybe_mark_memory_canary_deferred
maybe_mark_memory_canary_deferred(reason='canary_llm_slot', active_users=0)
" || true
  exit 0
fi

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
INSPECT=$(
  cd "${RMP_ROOT}" && ./venv/bin/python -c "
import json
from app.production.canary_sentinel import inspect_memory_canary_transcript
print(json.dumps(inspect_memory_canary_transcript('${TASK_ID}')))
"
)
SESSION_FILE=$(echo "$INSPECT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('session_file') or '')")
MEMORY_OK=$(echo "$INSPECT" | python3 -c "import json,sys; print(int(json.load(sys.stdin).get('memory_ok') or 0))")
PROMPT_OK=$(echo "$INSPECT" | python3 -c "import json,sys; print(int(json.load(sys.stdin).get('prompt_ok') or 0))")
SEARCH_BAD=$(echo "$INSPECT" | python3 -c "import json,sys; print(int(json.load(sys.stdin).get('search_bad') or 0))")
HAS_TRANSCRIPT=$(echo "$INSPECT" | python3 -c "import json,sys; print(int(bool(json.load(sys.stdin).get('has_transcript'))))")
echo "Transcript inspect: ${INSPECT}"

CTX=$(curl -sf -H "X-RMP-API-Key: ${API_KEY}" "http://127.0.0.1:8000/memory/process/${PROCESS_RUN}/context" 2>/dev/null || echo '{}')
echo "Memory context API count: $(echo "$CTX" | python3 -c "import json,sys; print(json.load(sys.stdin).get('count',0))" 2>/dev/null || echo 0)"

if [[ "$STATUS" != "completed" ]]; then
  echo "CANARY FAIL: task status=${STATUS}"
  if [[ -n "${TASK_ID}" ]]; then
    curl -sf -X POST -H "X-RMP-API-Key: ${API_KEY}" \
      "http://127.0.0.1:8000/tasks/${TASK_ID}/cancel?reason=canary_timeout" >/dev/null 2>&1 || true
  fi
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
  if [[ "${RMP_CANARY_SKIP_SENTINEL:-0}" != "1" ]]; then
    (
      cd "${RMP_ROOT}"
      ./venv/bin/python -m app.production.canary_sentinel --trigger memory_canary
    ) || true
  fi
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

RESULT_FILE="${RMP_ROOT}/data/last_memory_canary.json"
mkdir -p "$(dirname "${RESULT_FILE}")"
if [[ "${HAS_TRANSCRIPT}" -eq 0 ]]; then
  echo "CANARY INCONCLUSIVE: dispatch completed but transcript missing"
  python3 -c "
import json, datetime
print(json.dumps({
  'status': 'inconclusive',
  'dispatch_ok': 1,
  'task_id': '${TASK_ID}',
  'process_run_id': '${PROCESS_RUN}',
  'memory_ok': ${MEMORY_OK},
  'prompt_ok': ${PROMPT_OK},
  'search_bad': ${SEARCH_BAD},
  'session_file': '${SESSION_FILE}',
  'finished_at': datetime.datetime.utcnow().isoformat() + 'Z',
}))
" > "${RESULT_FILE}"
  if [[ "${RMP_CANARY_SKIP_SENTINEL:-0}" != "1" ]]; then
    (
      cd "${RMP_ROOT}"
      ./venv/bin/python -m app.production.canary_sentinel --trigger memory_canary
    ) || true
  fi
  exit 1
fi

if [[ "$MEMORY_OK" -ne 1 || "$PROMPT_OK" -ne 1 ]]; then
  echo "CANARY UNPROVEN: completed but memory_ok=${MEMORY_OK} prompt_ok=${PROMPT_OK}"
  python3 -c "
import json, datetime
print(json.dumps({
  'status': 'completed',
  'dispatch_ok': 1,
  'task_id': '${TASK_ID}',
  'process_run_id': '${PROCESS_RUN}',
  'memory_ok': ${MEMORY_OK},
  'prompt_ok': ${PROMPT_OK},
  'search_bad': ${SEARCH_BAD},
  'session_file': '${SESSION_FILE}',
  'finished_at': datetime.datetime.utcnow().isoformat() + 'Z',
}))
" > "${RESULT_FILE}"
  echo "Wrote ${RESULT_FILE} (not claiming recall is proven)"
  if [[ "${RMP_CANARY_SKIP_SENTINEL:-0}" != "1" ]]; then
    (
      cd "${RMP_ROOT}"
      ./venv/bin/python -m app.production.canary_sentinel --trigger memory_canary
    ) || true
  fi
  exit 1
fi

echo "CANARY OK (status=${STATUS}, memory_ok=${MEMORY_OK}, prompt_ok=${PROMPT_OK})"
python3 -c "
import json, datetime
print(json.dumps({
  'status': 'completed',
  'dispatch_ok': 1,
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
