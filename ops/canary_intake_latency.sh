#!/usr/bin/env bash
# Intake preview SLO: LLM path with confidence > 0 within budget wall-clock.
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
SETTINGS="${RMP_ROOT}/settings.json"
API="http://127.0.0.1:8000"
API_KEY="${RMP_API_KEY:-$(python3 -c "import json; print(json.load(open('${SETTINGS}'))['api_key'])")}"
# Context assembly + OpenClaw intake turn; keep above workflow activity budget headroom.
MAX_SEC=180

payload=$(python3 <<'PY'
import json
import time

# Unique intent avoids vector-gate short-circuit on prior greetings so we exercise
# the IntakeWorkflow LLM path (confidence > 0).
msg = (
    f"Aura latency canary {int(time.time())}: just saying hello, "
    "no task for you — how are you feeling?"
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

start=$(date +%s)
resp=$(curl -sf -X POST "${API}/tasks/intake/preview" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${API_KEY}" \
  -d "${payload}")
end=$(date +%s)
elapsed=$((end - start))

echo "${resp}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
intake = d.get('intake') or {}
mode = intake.get('execution_mode')
conf = int(intake.get('confidence') or 0)
assert conf > 0, f'expected confidence > 0 (LLM path), got {conf}; intake={intake!r}'
assert mode == 'conversational', f'expected conversational, got {mode!r}'
print(f'OK: execution_mode=conversational confidence={conf}')
"

if [[ "${elapsed}" -gt "${MAX_SEC}" ]]; then
  echo "FAIL: intake preview took ${elapsed}s (max ${MAX_SEC}s)" >&2
  exit 1
fi

echo "OK: intake preview latency ${elapsed}s (max ${MAX_SEC}s)"
echo "Intake latency canary PASS"
