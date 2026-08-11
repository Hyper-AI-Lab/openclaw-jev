#!/usr/bin/env bash
# Debounced restart of long-lived RMP Python processes after app/ code changes.
# Called by rmp-code-watch.service so disk edits cannot leave stale imports in memory.
set -euo pipefail

LOCK_FILE="${RMP_CODE_RELOAD_LOCK:-/run/rmp-code-reload.lock}"
LOG_FILE="${RMP_CODE_RELOAD_LOG:-/root/.openclaw/rmp/data/logs/code-reload.log}"
mkdir -p "$(dirname "${LOG_FILE}")"

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  # Another reload is already coalescing/running.
  exit 0
fi

# Coalesce multi-file edit bursts into one restart.
sleep "${RMP_CODE_RELOAD_DEBOUNCE_SEC:-3}"

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "${ts} rmp-code-reload: restarting rmp-api rmp-worker" >>"${LOG_FILE}"
systemctl restart rmp-api rmp-worker

ok=0
for _ in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 1
done

ts2="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [[ "${ok}" -eq 1 ]]; then
  echo "${ts2} rmp-code-reload: health OK" >>"${LOG_FILE}"
else
  echo "${ts2} rmp-code-reload: health check failed after restart" >>"${LOG_FILE}"
  exit 1
fi
