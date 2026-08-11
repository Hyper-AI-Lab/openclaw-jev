#!/usr/bin/env bash
# Reload systemd units and restart RMP + OpenClaw gateway.
set -euo pipefail

systemctl daemon-reload
systemctl restart rmp-api rmp-worker openclaw-gateway
sleep 3
curl -sf http://127.0.0.1:8000/health | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('status')=='ok', d"
# Confirm boot stamps exist (written by process startup)
python3 - <<'PY'
from pathlib import Path
root = Path("/root/.openclaw/rmp/data")
missing = [n for n in ("runtime_boot_rmp-api.json", "runtime_boot_rmp-worker.json") if not (root / n).exists()]
if missing:
    raise SystemExit(f"missing boot stamps: {missing}")
print("restart_rmp: boot stamps OK")
PY
echo "restart_rmp: health OK"
