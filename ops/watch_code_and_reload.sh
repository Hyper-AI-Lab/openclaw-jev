#!/usr/bin/env bash
# Recursively watch RMP Python sources and restart API/worker on change.
# systemd.path is non-recursive; inotify is required for app/**/*.py edits.
set -euo pipefail

RMP_ROOT="${RMP_ROOT:-/root/.openclaw/rmp}"
APP_DIR="${RMP_ROOT}/app"
WORKER_PY="${RMP_ROOT}/worker.py"
LOG_FILE="${RMP_ROOT}/data/logs/code-reload.log"
DEBOUNCE_SEC="${RMP_CODE_RELOAD_DEBOUNCE_SEC:-3}"
RELOAD_SCRIPT="${RMP_ROOT}/ops/reload_runtime_on_code_change.sh"

mkdir -p "$(dirname "$LOG_FILE")"

if ! command -v inotifywait >/dev/null 2>&1; then
  echo "inotifywait not installed (package inotify-tools)" >&2
  exit 1
fi

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) rmp-code-watch: watching ${APP_DIR} ${WORKER_PY}" >>"${LOG_FILE}"

# -q: quieter; -r recursive; -m monitor forever
# close_write|moved_to|create covers editor save patterns
inotifywait -m -r -e close_write,moved_to,create,delete \
  --exclude '(/__pycache__/|\.pyc$|/\.git/)' \
  "${APP_DIR}" "${WORKER_PY}" 2>>"${LOG_FILE}" | while read -r _directory _events _file; do
    # Coalesce bursts: drain rapidly queued events after first hit
    sleep "${DEBOUNCE_SEC}"
    while read -r -t 0.1 _; do :; done || true
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) rmp-code-watch: change detected — reloading" >>"${LOG_FILE}"
    /bin/bash "${RELOAD_SCRIPT}" >>"${LOG_FILE}" 2>&1 || true
  done
