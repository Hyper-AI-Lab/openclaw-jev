#!/usr/bin/env bash
# Staged production go-live — validates readiness, backs up, flips settings, restarts services.
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
SETTINGS="${RMP_ROOT}/settings.json"
VENV="${RMP_ROOT}/venv/bin/python"
API_KEY="$(python3 -c "import json; print(json.load(open('${SETTINGS}'))['api_key'])" 2>/dev/null || echo '')"

echo "=== RMP Production Go-Live ==="

echo "[1/6] Running backup..."
bash "${RMP_ROOT}/ops/backup.sh"

echo "[2/6] Running pytest..."
cd "${RMP_ROOT}"
if [[ -x ./venv/bin/pytest ]]; then
  ./venv/bin/pytest tests/ -q --tb=short || { echo "Tests failed — aborting go-live"; exit 1; }
else
  echo "WARN: pytest not installed — skipping tests"
fi

echo "[3/6] Production readiness check..."
READINESS="$("${VENV}" -c "
import asyncio, json
from app.production.readiness import run_all_checks
print(json.dumps(asyncio.run(run_all_checks()), indent=2))
")"
echo "${READINESS}"

FAILS="$(echo "${READINESS}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('blocking_failures',[])))")"
if [[ "${FAILS}" != "0" ]]; then
  echo "Blocking failures detected. Fix before go-live or use --force"
  if [[ "${1:-}" != "--force" ]]; then
    exit 1
  fi
  echo "Continuing with --force"
fi

echo "[4/6] Updating settings for production..."
python3 <<'PY'
import json
path = "/root/.openclaw/rmp/settings.json"
with open(path) as f:
    s = json.load(f)
s["development_mode"] = False
s["suspend_slack_notifications"] = False
s["suspend_task_interception"] = False
s["intermediate_updates"] = False
prod = s.setdefault("production", {})
prod["go_live_at"] = __import__("datetime").datetime.utcnow().isoformat() + "Z"
with open(path, "w") as f:
    json.dump(s, f, indent=2)
print("Settings updated: development_mode=false")
PY

echo "[5/6] Re-enabling OpenClaw heartbeat (30m)..."
python3 <<'PY'
import json
path = "/root/.openclaw/openclaw.json"
with open(path) as f:
    c = json.load(f)
c.setdefault("agents", {}).setdefault("defaults", {}).setdefault("heartbeat", {})["every"] = "30m"
with open(path, "w") as f:
    json.dump(c, f, indent=2)
print("Heartbeat set to 30m")
PY

echo "[6/6] Restarting services..."
systemctl restart temporal-dev.service || true
sleep 3
systemctl restart rmp-api.service rmp-worker.service openclaw-gateway.service

sleep 5
if [[ -n "${API_KEY}" ]]; then
  curl -sf -H "X-RMP-API-Key: ${API_KEY}" http://127.0.0.1:8000/health && echo ""
  curl -sf -H "X-RMP-API-Key: ${API_KEY}" http://127.0.0.1:8000/api/production/readiness | python3 -m json.tool | head -30
fi

echo "=== Go-live complete. Monitor for 72h soak per PRODUCTION_PLAN.md Phase 13 ==="
