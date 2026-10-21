#!/usr/bin/env bash
# RMP production backup: Postgres (RMP and Temporal), a Qdrant snapshot, artifacts, the OpenClaw stores, Aura's
# workspace notes and config snapshots. Every component runs even when one fails. manifest.json (format 2,
# app/production/backup_manifest.py) lists each component's files with their sizes and sha256, and the failures: each
# component that failed, or whose expected file is missing, empty or does not read back. After a failure no older
# backup is pruned, and the script exits 1, so rmp-backup.service shows the run as failed instead of passing.
# With settings' backup.offsite.enabled, ops/offsite_copy.sh then copies the backup off the host with restic; it stays
# off on this host, an accepted exception (K-7), and the manifest says "offsite": "off".
# Restore: ops/restore_backup.sh (runbook: docs/runbooks/backup-restore.md).
set -euo pipefail
umask 077

RMP_ROOT="${RMP_ROOT:-/root/.openclaw/rmp}"
DATA_DIR="${RMP_DATA_DIR:-${RMP_ROOT}/data}"
BACKUP_ROOT="${RMP_BACKUP_ROOT:-${DATA_DIR}/backups}"
PY="${RMP_PYTHON:-${RMP_ROOT}/venv/bin/python}"
OPENCLAW_HOME="${OPENCLAW_HOME:-/root/.openclaw}"
SETTINGS="${RMP_SETTINGS_PATH:-${RMP_ROOT}/settings.json}"
TEMPORAL_DBS="${RMP_TEMPORAL_DBS-temporal temporal_visibility}"
PG_DB="${PGDATABASE:-rmp_db}"
PG_USER="${PGUSER:-rmp}"
KEEP=14
# The off-host copy's repository, password and backend variables; read only when the copy is on.
RESTIC_ENV="${RMP_RESTIC_ENV:-/etc/rmp/restic.env}"
FAILURES=()
QDRANT_URL=""
QDRANT_SNAP=""

# The unit starts this script without a working directory: run from the checkout so `app` imports resolve.
cd "${RMP_ROOT}"
mkdir -p "${BACKUP_ROOT}"
# One backup at a time (the timer, `make backup` and ops/go_live.sh can overlap).
exec 9>"${BACKUP_ROOT}/.backup.lock"
flock -n 9 || { echo "another backup is running (${BACKUP_ROOT}/.backup.lock)" >&2; exit 1; }

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${BACKUP_ROOT}/${STAMP}"
LOG="${DEST}/backup.log"
mkdir -p "${DEST}"

log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "${LOG}"; }
fail() { FAILURES+=("$1"); log "FAIL: $1: $2"; }

# A snapshot this run created is a full copy of the vector store inside the container: remove it however the run ends.
cleanup() {
  rm -f "${DEST:-/nonexistent}"/*.partial
  if [[ -n "${QDRANT_SNAP}" ]]; then
    curl -fsS -m 60 -X DELETE "${QDRANT_URL}/snapshots/${QDRANT_SNAP}" >/dev/null 2>>"${LOG}" \
      || log "WARN: could not delete Qdrant snapshot ${QDRANT_SNAP}; the next run removes it"
    QDRANT_SNAP=""
  fi
}
trap cleanup EXIT
trap 'exit 143' TERM INT

log "Starting RMP backup → ${DEST}"

# Postgres: the RMP ledger. The rmp role's password lives only in DATABASE_URL (environment or /etc/rmp/rmp.env).
# Every dump waits at most 5 minutes for its table locks, then fails, instead of holding the backup lock all night.
PG_PASSWORD="${PGPASSWORD:-$("${PY}" -c 'from sqlalchemy.engine import make_url; from app.db.database import DATABASE_URL; print(make_url(DATABASE_URL).password or "")' 2>>"${LOG}" || true)}"
if command -v pg_dump >/dev/null 2>&1; then
  log "Dumping PostgreSQL ${PG_DB}..."
  PGPASSWORD="${PG_PASSWORD}" pg_dump -U "${PG_USER}" -h localhost --lock-wait-timeout=5min -Fc "${PG_DB}" -f "${DEST}/rmp_db.dump" 2>>"${LOG}" \
    || fail postgres "pg_dump failed (see backup.log)"
else
  fail postgres "pg_dump not found"
fi

# Postgres: Temporal's own databases (the live workflow store), through the postgres superuser's peer login.
for db in ${TEMPORAL_DBS}; do
  log "Dumping Temporal database ${db}..."
  if sudo -n -u postgres pg_dump --lock-wait-timeout=5min -Fc "${db}" > "${DEST}/${db}.dump.partial" 2>>"${LOG}" && [[ -s "${DEST}/${db}.dump.partial" ]]; then
    mv "${DEST}/${db}.dump.partial" "${DEST}/${db}.dump"
  else
    rm -f "${DEST}/${db}.dump.partial"
    fail "temporal:${db}" "pg_dump of ${db} failed"
  fi
done

# What the settings say of Qdrant (its mode, and its address or path), of the artifact store (on or off, and its root)
# and of the off-host copy (on or off). Read only: get_artifact_root would create a missing root.
CFG="$("${PY}" -c '
from app.config import get_artifact_store_config, get_vector_memory_config
from app.production.backup_manifest import offsite_config
c = get_vector_memory_config()
mode = (c.get("qdrant_mode") or "embedded").strip().lower()
server = mode == "server" or bool(c.get("qdrant_host"))
print("server" if server else "embedded")
print("http://%s:%d" % (c.get("qdrant_host") or "127.0.0.1", int(c.get("qdrant_port") or 6333)) if server else c.get("qdrant_path") or "")
a = get_artifact_store_config()
print("on" if a.get("enabled", True) else "off")
print(a.get("root_path") or "")
print("on" if offsite_config()["enabled"] else "off")
' 2>>"${LOG}" || true)"
QDRANT_MODE="$(sed -n 1p <<<"${CFG}")"
QDRANT_WHERE="$(sed -n 2p <<<"${CFG}")"
ARTIFACTS="$(sed -n 3p <<<"${CFG}")"
ARTIFACT_ROOT="$(sed -n 4p <<<"${CFG}")"
OFFSITE="$(sed -n 5p <<<"${CFG}")"

# Qdrant. In server mode a full snapshot through Qdrant's API is consistent, unlike a tar of live storage.
qdrant_snapshot() {
  local url="$1" part="${DEST}/qdrant-full.snapshot.partial" body stale got
  local -a meta
  QDRANT_URL="${url}"
  # After a boot the timer fires a missed run early: give Qdrant a minute to come up.
  curl -fsS -m 10 --retry 12 --retry-delay 5 --retry-all-errors -o /dev/null "${url}/readyz" 2>>"${LOG}" \
    || { fail qdrant "Qdrant at ${url} is not ready"; return 0; }
  # Full snapshots on the server come only from this script; any found now were left by a run that was killed.
  local listed
  listed="$(curl -fsS -m 30 "${url}/snapshots" 2>>"${LOG}")" || log "WARN: could not list Qdrant's full snapshots"
  for stale in $("${PY}" -c 'import json, sys; t = sys.stdin.read().strip(); print("\n".join(s["name"] for s in (json.loads(t).get("result") or []) if t))' <<<"${listed:-}" 2>>"${LOG}"); do
    curl -fsS -m 60 -X DELETE "${url}/snapshots/${stale}" >/dev/null 2>>"${LOG}" \
      && log "removed stale Qdrant snapshot ${stale}" || log "WARN: could not remove stale Qdrant snapshot ${stale}"
  done
  log "Snapshotting Qdrant at ${url}..."
  body="$(curl -fsS -m 900 -X POST "${url}/snapshots?wait=true" 2>>"${LOG}")" \
    || { fail qdrant "the full snapshot was not created"; return 0; }
  mapfile -t meta < <("${PY}" -c '
import json, sys
r = json.loads(sys.stdin.read())["result"]
print(r["name"]); print(int(r["size"])); print(r.get("checksum") or "")' <<<"${body}" 2>>"${LOG}")
  if (( ${#meta[@]} < 2 )) || [[ -z "${meta[0]}" ]]; then
    fail qdrant "unexpected snapshot response"
    return 0
  fi
  QDRANT_SNAP="${meta[0]}"
  if ! curl -fsS -m 900 -o "${part}" "${url}/snapshots/${QDRANT_SNAP}" 2>>"${LOG}"; then
    rm -f "${part}"; fail qdrant "the download of ${QDRANT_SNAP} failed"; return 0
  fi
  got="$(stat -c %s "${part}" 2>/dev/null || echo 0)"
  if [[ "${got}" != "${meta[1]}" ]]; then
    rm -f "${part}"; fail qdrant "the download is ${got} bytes, Qdrant reported ${meta[1]}"; return 0
  fi
  if [[ -n "${meta[2]:-}" && "$(sha256sum "${part}" | cut -d' ' -f1)" != "${meta[2]}" ]]; then
    rm -f "${part}"; fail qdrant "the download does not match Qdrant's checksum"; return 0
  fi
  if ! got="$(tar -tf "${part}" 2>>"${LOG}")" || [[ -z "${got}" ]]; then
    rm -f "${part}"; fail qdrant "the snapshot is not a readable archive"; return 0
  fi
  mv "${part}" "${DEST}/qdrant-full.snapshot"
  log "Qdrant snapshot ${QDRANT_SNAP}: $(du -h "${DEST}/qdrant-full.snapshot" | cut -f1)"
  cleanup
}

if [[ "${QDRANT_MODE}" == "server" && -n "${QDRANT_WHERE}" ]]; then
  qdrant_snapshot "${QDRANT_WHERE}"
elif [[ "${QDRANT_MODE}" == "embedded" && -d "${QDRANT_WHERE}" ]]; then
  log "Archiving embedded Qdrant data from ${QDRANT_WHERE}..."
  tar -czf "${DEST}/qdrant.tar.gz" -C "$(dirname "${QDRANT_WHERE}")" "$(basename "${QDRANT_WHERE}")" 2>>"${LOG}" \
    || fail qdrant "tar of ${QDRANT_WHERE} failed"
else
  fail qdrant "could not resolve Qdrant from settings (mode '${QDRANT_MODE}', '${QDRANT_WHERE}')"
fi

# Artifacts: the configured root, archived from its parent directory under its own name. A symlinked root is archived
# as the directory it resolves to, which the restore replaces: tar would store the link alone.
if [[ "${ARTIFACTS}" == off ]]; then
  log "The artifact store is disabled: no artifacts to archive"
elif [[ -d "${ARTIFACT_ROOT}" ]]; then
  log "Archiving artifacts from ${ARTIFACT_ROOT}..."
  ARTIFACT_DIR="$(realpath -- "${ARTIFACT_ROOT}" 2>>"${LOG}")" \
    && tar -czf "${DEST}/artifacts.tar.gz" -C "$(dirname "${ARTIFACT_DIR}")" "$(basename "${ARTIFACT_DIR}")" \
      2>>"${LOG}" || fail artifacts "tar of ${ARTIFACT_ROOT} failed"
else
  fail artifacts "the artifact root ${ARTIFACT_ROOT} is missing"
fi

# OpenClaw's agent and state stores (sessions, transcripts, auth, cron): SQLite backup API
log "Backing up OpenClaw stores..."
"${PY}" "${RMP_ROOT}/ops/backup_openclaw_state.py" backup --dest "${DEST}/openclaw-state" \
  >>"${LOG}" 2>&1 || fail openclaw-state "the OpenClaw store backup failed"

# Aura's workspace notes, without the git history, which can hold secrets, the moltmarket_venv virtualenv, the chat logs
# and log files. Restored by hand (docs/runbooks/backup-restore.md).
if [[ -d "${OPENCLAW_HOME}/workspace" ]]; then
  log "Archiving the OpenClaw workspace..."
  tar -czf "${DEST}/workspace.tar.gz" -C "${OPENCLAW_HOME}" --exclude=workspace/.git \
    --exclude=workspace/moltmarket_venv --exclude=workspace/chat_logs --exclude='*.log' workspace 2>>"${LOG}" \
    || fail workspace "tar of the workspace failed"
else
  fail workspace "${OPENCLAW_HOME}/workspace is missing"
fi

# Config snapshots: a missing one fails settings. OpenClaw's cron jobs live in its state store, copied above.
cp -a "${SETTINGS}" "${DEST}/settings.json" 2>>"${LOG}" || fail settings "could not copy ${SETTINGS}"
cp -a "${OPENCLAW_HOME}/openclaw.json" "${DEST}/openclaw.json" 2>>"${LOG}" \
  || fail settings "could not copy ${OPENCLAW_HOME}/openclaw.json"

# The manifest (app/production/backup_manifest.py, format 2): every component's files with their sizes and sha256,
# once each dump and archive has read back. A component whose expected file is missing, empty, unreadable or does not
# read back fails as well. Its stdout is the run's final failures, which decide the exit and the pruning; its stderr,
# the reason for each failure it adds, goes to the log and the journal.
log "Writing the manifest..."
MANIFEST_ARGS=()
for failure in "${FAILURES[@]+"${FAILURES[@]}"}"; do
  MANIFEST_ARGS+=(--failure "${failure}")
done
if written="$({ "${PY}" -m app.production.backup_manifest write "${DEST}" --temporal-dbs "${TEMPORAL_DBS}" \
    --qdrant-mode "${QDRANT_MODE}" --artifacts "${ARTIFACTS:-on}" "${MANIFEST_ARGS[@]+"${MANIFEST_ARGS[@]}"}" \
    2>&1 >&4 | tee -a "${LOG}" >&2; } 4>&1)"; then
  read -r -a FAILURES <<<"${written}"
else
  fail manifest "the manifest was not written"
fi

# The off-host copy (ops/offsite_copy.sh, restic), when the settings switch it on. Its configuration is read only from
# ${RESTIC_ENV}, which must belong to the user this runs as and be readable by no one else, and only in a subshell, so
# its secrets reach restic and no other step; restic's output goes to the log. It runs after a local failure too: what
# the run made is still worth having off the host. The manifest records off, copied or failed, and a failed copy fails
# the run.
OFFSITE_STATE=off
if [[ "${OFFSITE}" == on ]]; then
  log "Copying the backup off the host with restic..."
  OFFSITE_STATE=failed
  if [[ ! -f "${RESTIC_ENV}" ]]; then
    fail offsite "${RESTIC_ENV} is missing"
  elif OWNER_MODE="$(stat -c '%u %a' "${RESTIC_ENV}")"; [[ "${OWNER_MODE}" != "$(id -u) 600" ]]; then
    fail offsite "refusing ${RESTIC_ENV}: its owner and mode are '${OWNER_MODE}', not '$(id -u) 600'"
  elif ( set -a; . "${RESTIC_ENV}"; set +a; bash "${RMP_ROOT}/ops/offsite_copy.sh" "${DEST}" ) >>"${LOG}" 2>&1; then
    OFFSITE_STATE=copied
  else
    fail offsite "restic did not copy the backup (see backup.log)"
  fi
fi
log "Off-host copy: ${OFFSITE_STATE}"
# A run whose manifest was not written has failed as manifest already, and has nothing to record it in.
if [[ -f "${DEST}/manifest.json" ]]; then
  "${PY}" -m app.production.backup_manifest offsite "${DEST}" "${OFFSITE_STATE}" 2>>"${LOG}" \
    || fail manifest "the manifest does not record the off-host copy (${OFFSITE_STATE})"
fi

du -sh "${DEST}" | tee -a "${LOG}"
if (( ${#FAILURES[@]} )); then
  # A failed run prunes nothing: night after night of failures must never push out the last good backup.
  log "Backup INCOMPLETE: ${FAILURES[*]} failed (${DEST}); no older backup was pruned"
  exit 1
fi

# Retention: keep the newest ${KEEP} *complete* backups (manifest "failures": []), newest first by name. A failed
# backup never counts toward them, so a run of failures can't push out the last good backups; failed or unfinished
# backups older than the oldest kept complete one go with it. Only directories named like a backup stamp are
# touched, so anything else kept here (an OpenClaw rollback backup, a state copy) is never deleted by age.
log "Pruning old backups (keep ${KEEP} complete)..."
"${PY}" - "${BACKUP_ROOT}" "${KEEP}" <<'PY' 2>>"${LOG}" | xargs -r -d '\n' rm -rf
import json, re, sys
from pathlib import Path
root, keep = Path(sys.argv[1]), int(sys.argv[2])
dated = sorted((p for p in root.iterdir() if p.is_dir() and re.fullmatch(r"20\d{6}T\d{6}Z", p.name)), reverse=True)
def complete(p):
    try:
        return json.loads((p / "manifest.json").read_text()).get("failures") == []
    except (OSError, ValueError):
        return False
good = [p for p in dated if complete(p)]
if len(good) > keep:
    oldest_kept = good[keep - 1].name
    for p in dated:
        if p.name < oldest_kept:
            print(p)
PY
log "Backup complete: ${DEST}"
