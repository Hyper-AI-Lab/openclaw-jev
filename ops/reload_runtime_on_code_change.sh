#!/usr/bin/env bash
# Debounced restart of long-lived RMP Python processes after app/ code changes.
# Called by rmp-code-watch.service so disk edits cannot leave stale imports in memory.
# A restart kills in-flight activities, so it waits until no user task is active.
set -euo pipefail

RMP_ROOT="${RMP_ROOT:-/root/.openclaw/rmp}"
LOCK_FILE="${RMP_CODE_RELOAD_LOCK:-/run/rmp-code-reload.lock}"
LOG_FILE="${RMP_CODE_RELOAD_LOG:-${RMP_ROOT}/data/logs/code-reload.log}"
MAX_DEFER_SEC="${RMP_CODE_RELOAD_MAX_DEFER_SEC:-1800}"
POLL_SEC="${RMP_CODE_RELOAD_POLL_SEC:-15}"
HEALTH_URL="${RMP_CODE_RELOAD_HEALTH_URL:-http://127.0.0.1:8000/health}"
mkdir -p "$(dirname "${LOG_FILE}")"

log() {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) rmp-code-reload: $*" >>"${LOG_FILE}"
}

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  # Another reload is already coalescing/running.
  exit 0
fi

# Coalesce multi-file edit bursts into one restart.
sleep "${RMP_CODE_RELOAD_DEBOUNCE_SEC:-3}"

active_user_tasks() {
  # A failed lookup prints "unknown", which counts as busy.
  (cd "${RMP_ROOT}" && ./venv/bin/python -c \
    'from app.production.canary_sentinel import count_active_user_tasks_sync as c; print(c(strict=True))' \
    2>/dev/null) || echo "unknown"
}

waited=0
while :; do
  busy="$(active_user_tasks)"
  if [[ "${busy}" == "0" ]]; then
    break
  fi
  if (( waited >= MAX_DEFER_SEC )); then
    log "still busy (${busy}) after ${waited}s; the canary sentinel restarts stale runtimes once idle"
    exit 0
  fi
  if (( waited % 60 == 0 )); then
    log "deferring restart: ${busy} active user task(s)"
  fi
  sleep "${POLL_SEC}"
  waited=$(( waited + POLL_SEC ))
done

log "restarting rmp-api rmp-worker"
systemctl restart rmp-api rmp-worker

ok=0
for _ in $(seq 1 30); do
  if curl -sf "${HEALTH_URL}" >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 1
done

if [[ "${ok}" -eq 1 ]]; then
  log "health OK"
else
  log "health check failed after restart"
  exit 1
fi
