#!/usr/bin/env bash
# After agent edits under RMP app/plugin code, restart API/worker so in-memory
# modules cannot drift from disk (prevents ImportError / stale-canary outages).
set -euo pipefail

input=$(cat || true)
# Always allow the stop; restart is best-effort side effect.
DIRTY_FLAG="${RMP_CODE_DIRTY_FLAG:-/root/.openclaw/rmp/data/.code_dirty}"

if [[ -f "$DIRTY_FLAG" ]]; then
  rm -f "$DIRTY_FLAG"
  if [[ -x /root/.openclaw/rmp/ops/restart_rmp.sh ]]; then
    /root/.openclaw/rmp/ops/restart_rmp.sh >/tmp/rmp-stop-restart.log 2>&1 || true
  else
    systemctl restart rmp-api rmp-worker >/tmp/rmp-stop-restart.log 2>&1 || true
  fi
  echo '{"followup_message":"RMP code changed this session — restarted rmp-api and rmp-worker so runtime matches disk."}'
  exit 0
fi

echo '{}'
exit 0
