#!/usr/bin/env bash
# Intake preview must classify Slack chat DMs as conversational (Phase 9).
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
SETTINGS="${RMP_ROOT}/settings.json"
API="http://127.0.0.1:8000"
API_KEY="${RMP_API_KEY:-$(python3 -c "import json; print(json.load(open('${SETTINGS}'))['api_key'])")}"

payload=$(python3 <<'PY'
import json

msg = (
    "I am sorry for being quiet here, aura, I am just trying to strengthen your RMP "
    "and other things like temporal stack and also we have phoenix (projects manager), "
    "so the development goes quite extensively actually."
)
print(
    json.dumps(
        {
            "intent": msg,
            "session_key": "agent:main:main",
            "tags": ["user-request"],
            "user_id": "canary",
        }
    )
)
PY
)

resp=$(curl -sf -X POST "${API}/tasks/intake/preview" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${API_KEY}" \
  -d "${payload}")

echo "${resp}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
intake = d.get('intake') or {}
mode = intake.get('execution_mode')
assert mode == 'conversational', f'expected conversational, got {mode!r}; intake={intake!r}'
print('OK: intake preview execution_mode=conversational')
"

echo "Intake execution_mode canary PASS"
