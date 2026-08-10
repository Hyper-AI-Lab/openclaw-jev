#!/usr/bin/env bash
# Reload systemd units and restart RMP + OpenClaw gateway.
set -euo pipefail

systemctl daemon-reload
systemctl restart rmp-api rmp-worker openclaw-gateway
sleep 3
curl -sf http://127.0.0.1:8000/health | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('status')=='ok', d"
echo "restart_rmp: health OK"
