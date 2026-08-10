#!/usr/bin/env bash
# Roll back to development quiet mode.
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
SETTINGS="${RMP_ROOT}/settings.json"

echo "=== RMP Rollback to Development Mode ==="

python3 <<'PY'
import json
path = "/root/.openclaw/rmp/settings.json"
with open(path) as f:
    s = json.load(f)
s["development_mode"] = True
s["suspend_slack_notifications"] = True
s["suspend_task_interception"] = True
with open(path, "w") as f:
    json.dump(s, f, indent=2)
print("development_mode restored")
PY

python3 <<'PY'
import json
path = "/root/.openclaw/openclaw.json"
with open(path) as f:
    c = json.load(f)
c.setdefault("agents", {}).setdefault("defaults", {}).setdefault("heartbeat", {})["every"] = "0"
with open(path, "w") as f:
    json.dump(c, f, indent=2)
print("Heartbeat disabled")
PY

API_KEY="$(python3 -c "import json; print(json.load(open('${SETTINGS}'))['api_key'])")"
curl -sf -X POST -H "X-RMP-API-Key: ${API_KEY}" http://127.0.0.1:8000/dev/suspend-all || true

systemctl restart rmp-api.service rmp-worker.service openclaw-gateway.service
echo "Rollback complete"
