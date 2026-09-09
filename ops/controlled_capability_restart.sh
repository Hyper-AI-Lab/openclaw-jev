#!/usr/bin/env bash
# Gated self-upgrade restart: load new plugins / web backends safely.
# Does NOT restart RMP mid-task by default (would risk aborting the upgrade workflow).
#
# Usage:
#   bash ops/controlled_capability_restart.sh              # gateway only
#   bash ops/controlled_capability_restart.sh --web        # + aura-web-backends
#   bash ops/controlled_capability_restart.sh --gateway --web
#   bash ops/controlled_capability_restart.sh --rmp-if-idle
#   bash ops/controlled_capability_restart.sh --force-rmp  # explicit; may interrupt workflows
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
PENDING_RMP="${RMP_ROOT}/data/pending_capability_rmp_restart.json"
DO_GATEWAY=0
DO_WEB=0
DO_RMP_IDLE=0
DO_RMP_FORCE=0

if [[ $# -eq 0 ]]; then
  DO_GATEWAY=1
fi

for arg in "$@"; do
  case "$arg" in
    --gateway) DO_GATEWAY=1 ;;
    --web) DO_WEB=1 ;;
    --rmp-if-idle) DO_RMP_IDLE=1 ;;
    --force-rmp) DO_RMP_FORCE=1 ;;
    --all-safe)
      DO_GATEWAY=1
      DO_WEB=1
      DO_RMP_IDLE=1
      ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

ACTIVE_USERS=$(
  cd "${RMP_ROOT}" && ./venv/bin/python -c "
from app.production.canary_sentinel import count_active_user_tasks_sync
print(count_active_user_tasks_sync())
" 2>/dev/null || echo 0
)
echo "controlled_capability_restart: active_user_tasks=${ACTIVE_USERS}"

RESTARTED=()
DEFERRED=()

if [[ "${DO_GATEWAY}" -eq 1 ]]; then
  systemctl restart openclaw-gateway
  RESTARTED+=("openclaw-gateway")
  sleep 2
  systemctl is-active --quiet openclaw-gateway
  echo "controlled_capability_restart: openclaw-gateway active"
fi

if [[ "${DO_WEB}" -eq 1 ]]; then
  systemctl restart aura-web-backends
  RESTARTED+=("aura-web-backends")
  ok=0
  for _ in $(seq 1 20); do
    if curl -sf http://127.0.0.1:8791/health >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 1
  done
  if [[ "${ok}" -ne 1 ]]; then
    echo "controlled_capability_restart: aura-web-backends health FAILED" >&2
    exit 1
  fi
  echo "controlled_capability_restart: aura-web-backends health OK"
fi

maybe_restart_rmp() {
  systemctl restart rmp-api rmp-worker
  RESTARTED+=("rmp-api" "rmp-worker")
  ok=0
  for _ in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 1
  done
  if [[ "${ok}" -ne 1 ]]; then
    echo "controlled_capability_restart: rmp health FAILED" >&2
    exit 1
  fi
  echo "controlled_capability_restart: rmp health OK"
  rm -f "${PENDING_RMP}"
}

if [[ "${DO_RMP_FORCE}" -eq 1 ]]; then
  echo "controlled_capability_restart: FORCE restarting RMP (may interrupt active workflows)"
  maybe_restart_rmp
elif [[ "${DO_RMP_IDLE}" -eq 1 ]]; then
  if [[ "${ACTIVE_USERS}" =~ ^[1-9][0-9]*$ ]]; then
    mkdir -p "$(dirname "${PENDING_RMP}")"
    python3 - <<PY
import json
from datetime import datetime, timezone
from pathlib import Path
Path("${PENDING_RMP}").write_text(json.dumps({
  "requested_at": datetime.now(timezone.utc).isoformat(),
  "reason": "capability_upgrade",
  "active_user_tasks": int("${ACTIVE_USERS}"),
  "command": "bash /root/.openclaw/rmp/ops/restart_rmp.sh",
}, indent=2))
print("wrote ${PENDING_RMP}")
PY
    DEFERRED+=("rmp-api/rmp-worker")
    echo "controlled_capability_restart: RMP restart DEFERRED (${ACTIVE_USERS} active user task(s))"
  else
    maybe_restart_rmp
  fi
fi

echo "---"
echo "restarted: ${RESTARTED[*]:-(none)}"
echo "deferred: ${DEFERRED[*]:-(none)}"
echo "controlled_capability_restart: OK"
