#!/usr/bin/env bash
# Post-upgrade verify: health + optional pytest + soft canary status (no competing canary).
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
FAIL=0

echo "=== verify_capability_upgrade ==="

check() {
  local name="$1"
  shift
  if "$@"; then
    echo "OK  ${name}"
  else
    echo "FAIL ${name}" >&2
    FAIL=1
  fi
}

check "rmp /health" curl -sf http://127.0.0.1:8000/health >/dev/null
check "openclaw-gateway active" systemctl is-active --quiet openclaw-gateway

if systemctl list-unit-files aura-web-backends.service >/dev/null 2>&1; then
  if systemctl is-active --quiet aura-web-backends; then
    check "web-stack /health" curl -sf http://127.0.0.1:8791/health >/dev/null
  else
    echo "SKIP web-stack (aura-web-backends inactive)"
  fi
fi

ACTIVE_USERS=$(
  cd "${RMP_ROOT}" && ./venv/bin/python -c "
from app.production.canary_sentinel import count_active_user_tasks_sync
print(count_active_user_tasks_sync())
" 2>/dev/null || echo 0
)
echo "active_user_tasks=${ACTIVE_USERS}"

# Soft canary: do not start a competing hourly canary while users are active.
if [[ "${ACTIVE_USERS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "SKIP live canary (active user task(s)); checking last canary file"
  if [[ -f "${RMP_ROOT}/data/last_health_canary.json" ]]; then
    python3 - <<'PY'
import json
from pathlib import Path
p = Path("/root/.openclaw/rmp/data/last_health_canary.json")
d = json.loads(p.read_text())
print("last_health_canary:", d.get("status"), "at", d.get("updated_at") or d.get("completed_at") or d)
PY
  else
    echo "WARN no last_health_canary.json yet"
  fi
else
  echo "Running soft canary (idle)…"
  if bash "${RMP_ROOT}/ops/canary.sh"; then
    echo "OK  canary"
  else
    echo "FAIL canary" >&2
    FAIL=1
  fi
fi

if [[ "${VERIFY_RUN_PYTEST:-1}" == "1" ]]; then
  echo "Running focused pytest…"
  if (
    cd "${RMP_ROOT}" && ./venv/bin/pytest -q \
      tests/test_catalog.py \
      tests/test_web_capability.py \
      tests/test_evidence.py
  ); then
    echo "OK  pytest focused"
  else
    echo "FAIL pytest focused" >&2
    FAIL=1
  fi
fi

if [[ -f "${RMP_ROOT}/data/pending_capability_rmp_restart.json" ]]; then
  echo "NOTE pending RMP restart:"
  cat "${RMP_ROOT}/data/pending_capability_rmp_restart.json"
fi

if [[ "${FAIL}" -ne 0 ]]; then
  echo "verify_capability_upgrade: FAILED"
  exit 1
fi
echo "verify_capability_upgrade: OK"
echo "hint: call tool web_capability_status for plugin/backend inventory"
