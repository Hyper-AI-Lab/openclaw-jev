#!/usr/bin/env bash
# Intake LLM-path SLO: the preview bypasses Jev, so this measures context assembly
# plus the OpenClaw intake LLM turn (confidence > 0) against a 45 s target.
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
SETTINGS="${RMP_ROOT}/settings.json"
API="http://127.0.0.1:8000"
API_KEY="${RMP_API_KEY:-$(python3 -c "import json; print(json.load(open('${SETTINGS}'))['api_key'])")}"
MAX_SEC=45

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
resp=$(curl -sf -X POST "${API}/tasks/intake/preview?bypass_jev=true" \
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
raw = intake.get('llm_raw') or {}
assert 'jev' not in raw and raw.get('decision_source') != 'jev', f'expected the LLM path, got Jev; intake={intake!r}'
assert conf > 0, f'expected confidence > 0 (LLM path), got {conf}; intake={intake!r}'
assert mode == 'conversational', f'expected conversational, got {mode!r}'
print(f'OK: LLM path (Jev bypassed) execution_mode=conversational confidence={conf}')
"

if [[ "${elapsed}" -gt "${MAX_SEC}" ]]; then
  echo "FAIL: intake preview took ${elapsed}s (max ${MAX_SEC}s)" >&2
  exit 1
fi

echo "OK: intake LLM-path latency ${elapsed}s (max ${MAX_SEC}s)"
echo "Intake latency canary PASS"
