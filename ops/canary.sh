#!/usr/bin/env bash
# Hourly RMP canary — lightweight task proving OpenClaw dispatch path is alive.
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
SETTINGS="${RMP_ROOT}/settings.json"
API_KEY="${RMP_API_KEY:-$(python3 -c "import json; print(json.load(open('${SETTINGS}'))['api_key'])")}"
HOUR="$(date -u +%Y%m%dT%H%M)"
KEY="canary:${HOUR}"
RESULT_FILE="${RMP_ROOT}/data/last_health_canary.json"

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
  (
    cd "${RMP_ROOT}"
    ./venv/bin/python -m app.production.canary_sentinel --trigger health_canary
  ) || true
}

RESP=$(curl -sf -X POST "http://127.0.0.1:8000/tasks" \
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
")")

TASK_ID=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('task_id',''))")
echo "Canary task created: ${TASK_ID}"

# Poll up to 6 minutes (canary workflow includes dispatch + validation)
for i in $(seq 1 36); do
  sleep 10
  STATUS=$(curl -sf -H "X-RMP-API-Key: ${API_KEY}" "http://127.0.0.1:8000/tasks/${TASK_ID}" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))")
  echo "  poll ${i}: ${STATUS}"
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
run_sentinel
exit 1
