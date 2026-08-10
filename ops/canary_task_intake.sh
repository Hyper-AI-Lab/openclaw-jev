#!/usr/bin/env bash
# Shadow intake canary — verifies intake preview returns structured decisions.
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
SETTINGS="${RMP_ROOT}/settings.json"
API="http://127.0.0.1:8000"
API_KEY="${RMP_API_KEY:-$(python3 -c "import json; print(json.load(open('${SETTINGS}'))['api_key'])")}"

payload='{"intent":"Check server health and report status","session_key":"agent:main:main","tags":["user-request"],"user_id":"canary"}'

resp=$(curl -sf -X POST "${API}/tasks/intake/preview" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${API_KEY}" \
  -d "${payload}")

echo "${resp}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
intake = d.get('intake') or {}
assert intake.get('decision') or intake.get('effective_decision'), 'missing decision'
print('OK: intake preview decision=', intake.get('decision'), 'mode=', intake.get('intake_mode'))
"

echo "Task intake canary PASS"
