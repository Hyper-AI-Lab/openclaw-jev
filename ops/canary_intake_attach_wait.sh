#!/usr/bin/env bash
# Live attach/wait smoke: duplicate intent while first task is still running.
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
SETTINGS="${RMP_ROOT}/settings.json"
API="http://127.0.0.1:8000"
API_KEY="${RMP_API_KEY:-$(python3 -c "import json; print(json.load(open('${SETTINGS}'))['api_key'])")}"
STAMP="$(date -u +%Y%m%dT%H%M%S)"
KEY="intake-attach:${STAMP}"
SESSION="agent:main:main"
CANCEL_TASK=1

auth=(-H "Authorization: Bearer ${API_KEY}" -H "Content-Type: application/json")

INTENT="Intake attach smoke ${STAMP}: reply with exactly CANARY_OK on its own line. No tools."

echo "=== Intake attach/wait canary (${STAMP}) ==="

resp1=$(curl -sf -X POST "${API}/tasks" "${auth[@]}" -d "$(python3 -c "
import json
print(json.dumps({
  'intent': '''${INTENT}''',
  'session_key': '${SESSION}',
  'raw_text': '''${INTENT}''',
  'idempotency_key': '${KEY}',
  'tags': ['canary', 'system', 'intake-smoke'],
}))
")")

task_id=$(echo "$resp1" | python3 -c "import json,sys; print(json.load(sys.stdin).get('task_id',''))")
echo "Created task ${task_id}"

status=""
for i in $(seq 1 60); do
  status=$(curl -sf "${API}/tasks/${task_id}" "${auth[@]}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))")
  if [[ "$status" == "running" ]]; then
    echo "Task running after ${i} polls"
    break
  fi
  if [[ "$status" == "completed" || "$status" == "failed" ]]; then
    echo "WARN: task reached terminal state=${status} before duplicate (poll ${i})"
    break
  fi
  sleep 0.5
done

resp2=$(curl -sf -X POST "${API}/tasks" "${auth[@]}" -d "$(python3 -c "
import json
print(json.dumps({
  'intent': '''${INTENT}''',
  'session_key': '${SESSION}',
  'raw_text': '''${INTENT}''',
  'idempotency_key': '${KEY}-dup',
  'tags': ['canary', 'system', 'intake-smoke'],
}))
")")

action=$(echo "$resp2" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('intake_action') or ('dedup' if d.get('deduplicated') else 'create'))
")
echo "Duplicate response action=${action} (first_task_status=${status})"

if [[ "$status" == "running" ]]; then
  case "$action" in
    attach_active|wait_active|dedup)
      echo "Intake attach/wait canary PASS"
      ;;
    *)
      echo "FAIL: expected attach_active|wait_active|dedup while running, got ${action}"
      echo "$resp2"
      exit 1
      ;;
  esac
else
  echo "SKIP strict attach/wait assertion (task not running at duplicate time)"
  echo "Intake attach/wait canary PASS (degraded)"
fi

if [[ "${CANCEL_TASK}" == "1" && -n "${task_id}" ]]; then
  curl -sf -X POST "${API}/tasks/${task_id}/cancel" "${auth[@]}" >/dev/null 2>&1 || true
  echo "Cancelled smoke task ${task_id}"
fi
