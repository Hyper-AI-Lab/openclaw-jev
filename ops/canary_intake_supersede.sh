#!/usr/bin/env bash
# Intake supersede canary: stale failed recurrent registry → supersede decision.
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
SETTINGS="${RMP_ROOT}/settings.json"
API="http://127.0.0.1:8000"
API_KEY="${RMP_API_KEY:-$(python3 -c "import json; print(json.load(open('${SETTINGS}'))['api_key'])")}"
STAMP="$(date -u +%Y%m%dT%H%M%S)"
SESSION="agent:main:cron:canary"
INTENT="[cron:SupersedeCanary-${STAMP}] check notifications after failure"

auth=(-H "Authorization: Bearer ${API_KEY}" -H "Content-Type: application/json")

echo "=== Intake supersede canary (${STAMP}) ==="

REC_KEY=$(cd "${RMP_ROOT}" && ./venv/bin/python -c "
from app.task_registry.recurrence import derive_recurrence_key
print(derive_recurrence_key('${SESSION}', '''${INTENT}''', ['cron']) or '')
")
echo "Recurrence key=${REC_KEY}"

cd "${RMP_ROOT}"
./venv/bin/python <<PY
import asyncio
import uuid
from datetime import datetime, timedelta

from app.db.database import AsyncSessionLocal
from app.db.models import Task, TaskRegistryEntry

REC_KEY = "${REC_KEY}"
INTENT = """${INTENT}"""


async def seed():
    task_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        db.add(
            Task(
                id=task_id,
                correlation_id=task_id,
                idempotency_key=f"supersede-canary-seed:{task_id}",
                requester="canary",
                openclaw_session_key="${SESSION}",
                task_type="cron",
                goal=INTENT,
                status="failed",
                task_kind="recurrent",
                recurrence_key=REC_KEY,
            )
        )
        db.add(
            TaskRegistryEntry(
                id=str(uuid.uuid4()),
                task_id=task_id,
                intent_snippet=INTENT,
                outcome_summary="Workflow failed: timeout",
                process_type="cron",
                terminal_status="failed",
                task_kind="recurrent",
                recurrence_key=REC_KEY,
                session_key="${SESSION}",
                task_ended_at=datetime.utcnow() - timedelta(hours=2),
                indexed_at=datetime.utcnow(),
            )
        )
        await db.commit()
    print(task_id)

asyncio.run(seed())
PY

resp=$(curl -sf -X POST "${API}/tasks/intake/preview" "${auth[@]}" -d "$(python3 -c "
import json
print(json.dumps({
  'intent': '''${INTENT}''',
  'session_key': '${SESSION}',
  'tags': ['cron'],
}))
")")

decision=$(echo "$resp" | python3 -c "
import json,sys
d=json.load(sys.stdin)
intake=d.get('intake') or d
print(intake.get('effective_decision') or intake.get('decision',''))
")
echo "Preview decision=${decision}"

if [[ "$decision" != "supersede" ]]; then
  echo "FAIL: expected supersede, got ${decision}"
  echo "$resp"
  exit 1
fi

echo "Intake supersede canary PASS"
