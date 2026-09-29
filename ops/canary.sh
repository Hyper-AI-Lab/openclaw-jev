#!/usr/bin/env bash
# Hourly RMP canary — lightweight task proving OpenClaw dispatch path is alive.
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
SETTINGS="${RMP_ROOT}/settings.json"
API_KEY="${RMP_API_KEY:-$(python3 -c "import json; print(json.load(open('${SETTINGS}'))['api_key'])")}"
HOUR="$(date -u +%Y%m%dT%H%M)"
KEY="canary:${HOUR}"
RESULT_FILE="${RMP_ROOT}/data/last_health_canary.json"

# Soft skip: do not compete with live user work (prevents LLM starvation → false timeout).
ACTIVE_USERS=$(
  cd "${RMP_ROOT}" && ./venv/bin/python -c "
from app.production.canary_sentinel import count_active_user_tasks_sync
print(count_active_user_tasks_sync())
" 2>/dev/null || echo 0
)
if [[ "${ACTIVE_USERS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "CANARY SKIP: ${ACTIVE_USERS} active user task(s) — deferring hourly canary"
  cd "${RMP_ROOT}" && ./venv/bin/python -c "
from app.production.canary_sentinel import maybe_mark_health_canary_deferred
maybe_mark_health_canary_deferred(reason='active_user_tasks', active_users=int('${ACTIVE_USERS}'))
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
  echo "CANARY SKIP: ${USER_SLOTS} user LLM slot(s) in use — deferring hourly canary"
  cd "${RMP_ROOT}" && ./venv/bin/python -c "
from app.production.canary_sentinel import maybe_mark_health_canary_deferred
maybe_mark_health_canary_deferred(reason='user_llm_slots', active_users=int('${USER_SLOTS}'))
" || true
  exit 0
fi

write_result() {
  local status="$1"
  local task_id="${2:-}"
  local error="${3:-}"
  mkdir -p "$(dirname "${RESULT_FILE}")"
  (
    cd "${RMP_ROOT}"
    ./venv/bin/python -c "
from app.production.canary_sentinel import write_health_canary_result
write_health_canary_result(status='${status}', task_id='${task_id}', error='''${error}''')
"
  )
}

run_sentinel() {
  if [[ "${RMP_CANARY_SKIP_SENTINEL:-0}" == "1" ]]; then
    return 0
  fi
  (
    cd "${RMP_ROOT}"
    ./venv/bin/python -m app.production.canary_sentinel --trigger health_canary
  ) || true
}

RESP=$(curl -sS -w "\n%{http_code}" -X POST "http://127.0.0.1:8000/tasks" \
  -H "Content-Type: application/json" \
  -H "X-RMP-API-Key: ${API_KEY}" \
  -d "$(python3 -c "
import json
print(json.dumps({
  'intent': 'RMP CANARY: Reply with exactly CANARY_OK on its own line. No tools.',
  'session_key': 'agent:main:main',
  'idempotency_key': '${KEY}',
  'tags': ['canary', 'system'],
}))
")" || true)
HTTP_CODE=$(printf '%s' "$RESP" | tail -n1)
BODY=$(printf '%s' "$RESP" | sed '$d')
if [[ -z "$HTTP_CODE" || "$HTTP_CODE" != "200" || -z "$BODY" ]]; then
  err="task create failed http=${HTTP_CODE:-none} body=${BODY:0:200}"
  echo "CANARY FAIL: ${err}"
  write_result failed "" "${err}"
  run_sentinel
  exit 1
fi
RESP="$BODY"

TASK_ID=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('task_id',''))")
echo "Canary task created: ${TASK_ID}"

# Poll up to 6 minutes (canary workflow includes dispatch + validation)
MAX_POLLS="${RMP_CANARY_MAX_POLLS:-36}"
for i in $(seq 1 "${MAX_POLLS}"); do
  sleep 10
  BODY=$(curl -sf -H "X-RMP-API-Key: ${API_KEY}" "http://127.0.0.1:8000/tasks/${TASK_ID}" || true)
  if [[ -z "${BODY}" ]]; then
    echo "  poll ${i}: empty response"
    continue
  fi
  STATUS=$(printf '%s' "${BODY}" | python3 -c "import json,sys
try:
    print(json.load(sys.stdin).get('status',''))
except Exception:
    print('')" || true)
  echo "  poll ${i}: ${STATUS:-unparsed}"
  if [[ "$STATUS" == "completed" ]]; then
    write_result completed "${TASK_ID}"
    echo "CANARY OK"
    exit 0
  fi
  if [[ "$STATUS" == "failed" ]]; then
    write_result failed "${TASK_ID}" "task failed"
    echo "CANARY FAIL: task failed"
    run_sentinel
    exit 1
  fi
done

write_result timeout "${TASK_ID}" "poll timeout"
echo "CANARY TIMEOUT"
# Cancel the stuck canary task so it cannot pin LLM slots / starve user work.
if [[ -n "${TASK_ID}" ]]; then
  curl -sf -X POST -H "X-RMP-API-Key: ${API_KEY}" \
    "http://127.0.0.1:8000/tasks/${TASK_ID}/cancel?reason=canary_timeout" >/dev/null 2>&1 || true
fi
run_sentinel
exit 1
