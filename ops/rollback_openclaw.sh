#!/usr/bin/env bash
# Roll OpenClaw back to the version and data from before an upgrade, from the backup
# ops/upgrade_openclaw.sh took (openclaw-update-*/).
#
#   bash ops/rollback_openclaw.sh [BACKUP_DIR] [--force]   # default: the last upgrade's backup
#   ROLLBACK_TARGET=staging bash ops/rollback_openclaw.sh  # the rehearsal on the staging gateway
#
# Refuses while user tasks run (unless --force), then: stop the gateway, reinstall the version
# from before, restore the stores (backup-API copies), restore config, local plugins and workspace
# files, reinstall each npm plugin at its version from before (2026.9.7's update deletes the old
# plugin generations), settle sessions, patch and verify, start, and wait for /readyz.
set -euo pipefail

RMP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${RMP_ROOT}/venv/bin/python"
TARGET="${ROLLBACK_TARGET:-production}"
FORCE=0
BACKUP_DIR=""
for arg in "$@"; do
  case "${arg}" in
    --force) FORCE=1 ;;
    *) BACKUP_DIR="${arg}" ;;
  esac
done

log() { echo "[rollback:${TARGET}] $(date -u +%H:%M:%S) $*"; }
die() { log "ERROR: $*"; exit 1; }

if [[ "${TARGET}" == "staging" ]]; then
  STAGING=("${PY}" "${RMP_ROOT}/ops/openclaw_staging.py")
  OC_HOME="/srv/openclaw-staging/home/.openclaw"
  BACKUP_DIR="${BACKUP_DIR:-/srv/openclaw-staging/upgrade-backup}"
  PORT=19789
  gw_stop() { "${STAGING[@]}" stop; }
  gw_start() { "${STAGING[@]}" start; }
  oc() { "${STAGING[@]}" cli -- "$@"; }
  install_core() { "${STAGING[@]}" install "$1"; }
  restore_stores() { "${STAGING[@]}" restore-pre; }
  dist_dir() { echo "/srv/openclaw-staging/npm-global/lib/node_modules/openclaw/dist"; }
elif [[ "${TARGET}" == "production" ]]; then
  OC_HOME="/root/.openclaw"
  BACKUP_DIR="${BACKUP_DIR:-$(cat /tmp/openclaw-upgrade-backup.path 2>/dev/null || true)}"
  PORT="$(python3 -c "import json; print(json.load(open('${OC_HOME}/openclaw.json'))['gateway']['port'])")"
  gw_stop() { systemctl stop openclaw-gateway; }
  gw_start() { systemctl start openclaw-gateway; }
  oc() { openclaw "$@"; }
  install_core() {
    npm install -g "openclaw@$1" || npm install -g "openclaw@$1" --allow-scripts=openclaw
    bash "${RMP_ROOT}/patch_openclaw.sh"
  }
  restore_stores() { "${PY}" "${RMP_ROOT}/ops/backup_openclaw_state.py" restore "${BACKUP_DIR}/openclaw-state" --yes; }
  dist_dir() { echo "$(dirname "$(readlink -f "$(command -v openclaw)")")/dist"; }
else
  die "ROLLBACK_TARGET must be production or staging"
fi

[[ -n "${BACKUP_DIR}" && -f "${BACKUP_DIR}/VERSIONS.txt" ]] || die "no upgrade backup with VERSIONS.txt (${BACKUP_DIR:-none given})"
BEFORE="$(sed -n 's/^openclaw_before=//p' "${BACKUP_DIR}/VERSIONS.txt" | grep -oE '[0-9]{4}\.[0-9]+\.[0-9]+' | head -1)"
[[ -n "${BEFORE}" ]] || die "VERSIONS.txt names no openclaw_before version"
mapfile -t PLUGINS < <(sed -n 's/^plugin_before=//p' "${BACKUP_DIR}/VERSIONS.txt")
log "backup ${BACKUP_DIR}: openclaw ${BEFORE}, plugins ${PLUGINS[*]:-none}"

if [[ "${TARGET}" == "production" && "${FORCE}" != "1" ]]; then
  ACTIVE="$(cd "${RMP_ROOT}" && "${PY}" -c "from app.production.canary_sentinel import count_active_user_tasks_sync; print(count_active_user_tasks_sync())")"
  [[ "${ACTIVE}" =~ ^[0-9]+$ ]] || die "could not count active user tasks (got: ${ACTIVE:-empty})"
  [[ "${ACTIVE}" -eq 0 ]] || die "refusing: ${ACTIVE} active user task(s); retry when idle or pass --force"
fi

started=$(date +%s)
log "stopping the gateway"
gw_stop

log "reinstalling openclaw@${BEFORE}"
install_core "${BEFORE}"

log "restoring the stores"
restore_stores

log "restoring config, local plugins and workspace files"
if [[ -f "${BACKUP_DIR}/openclaw.json" ]]; then
  cp -a "${BACKUP_DIR}/openclaw.json" "${OC_HOME}/openclaw.json"
fi
for plug in "${BACKUP_DIR}"/plugins/*/; do
  [[ -d "${plug}" ]] || continue
  name="$(basename "${plug}")"
  rm -rf "${OC_HOME:?}/plugins/${name}"
  cp -a "${plug%/}" "${OC_HOME}/plugins/${name}"
done
for file in TOOLS.md AGENTS.md; do
  if [[ -f "${BACKUP_DIR}/workspace/${file}" ]]; then
    cp -a "${BACKUP_DIR}/workspace/${file}" "${OC_HOME}/workspace/${file}"
  fi
done

for spec in "${PLUGINS[@]}"; do
  log "reinstalling plugin ${spec}"
  oc plugins install "${spec}" --force --pin
done

log "settling session rows"
"${PY}" "${RMP_ROOT}/ops/settle_openclaw_sessions.py" "${OC_HOME}/agents/main/agent/openclaw-agent.sqlite"

log "verifying RMP's patches"
python3 "${RMP_ROOT}/ops/openclaw_patch_audit.py" "$(dist_dir)"

log "starting the gateway"
gw_start
for _ in $(seq 1 180); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${PORT}/readyz" || true)"
  [[ "${code}" == "200" ]] && break
  sleep 2
done
[[ "${code}" == "200" ]] || die "gateway not ready after the rollback (readyz ${code})"
log "rolled back to openclaw ${BEFORE} in $(( $(date +%s) - started )) s; ready on port ${PORT}"
