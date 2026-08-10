#!/usr/bin/env bash
# Live intake canary: create → duplicate intent (wait/attach) → verify audit row.
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
SETTINGS="${RMP_ROOT}/settings.json"
API="http://127.0.0.1:8000"
API_KEY="${RMP_API_KEY:-$(python3 -c "import json; print(json.load(open('${SETTINGS}'))['api_key'])")}"
STAMP="$(date -u +%Y%m%dT%H%M%S)"
KEY="intake-live:${STAMP}"

auth=(-H "Authorization: Bearer ${API_KEY}" -H "Content-Type: application/json")

echo "=== Intake live canary (${STAMP}) ==="

INTENT="Intake live canary: reply with one word OK"

resp1=$(curl -sf -X POST "${API}/tasks" "${auth[@]}" -d "$(python3 -c "
import json
print(json.dumps({
  'intent': '${INTENT}',
  'session_key': 'agent:main:main',
  'raw_text': '${INTENT}',
  'idempotency_key': '${KEY}',
  'tags': ['canary', 'user-request'],
}))
")")

task_id=$(echo "$resp1" | python3 -c "import json,sys; print(json.load(sys.stdin).get('task_id',''))")
echo "Created task ${task_id}"

resp2=$(curl -sf -X POST "${API}/tasks" "${auth[@]}" -d "$(python3 -c "
import json
print(json.dumps({
  'intent': '${INTENT}',
  'session_key': 'agent:main:main',
  'raw_text': '${INTENT}',
  'idempotency_key': '${KEY}-dup',
  'tags': ['canary', 'user-request'],
}))
")")

action=$(echo "$resp2" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('intake_action') or ('dedup' if d.get('deduplicated') else 'create'))
")
echo "Duplicate response action=${action}"

cd "${RMP_ROOT}"
count=$(./venv/bin/python -c "
import asyncio
from sqlalchemy import select, func
from app.db.database import AsyncSessionLocal
from app.db.models import TaskIntakeDecision
async def main():
    async with AsyncSessionLocal() as db:
        n = await db.scalar(select(func.count()).select_from(TaskIntakeDecision))
    print(n or 0)
asyncio.run(main())
")
echo "Intake decisions in DB: ${count}"
test "${count}" -gt 0

echo "Intake live canary PASS"
