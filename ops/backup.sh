#!/usr/bin/env bash
# RMP production backup: Postgres (RMP and Temporal), a Qdrant snapshot, artifacts, the OpenClaw stores and config
# snapshots. Every component runs even when one fails; the failures are listed in manifest.json, no older backup
# is pruned, and the script exits 1, so rmp-backup.service shows the run as failed instead of passing.
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
PG_PASSWORD="${PGPASSWORD:-$("${PY}" -c 'from sqlalchemy.engine import make_url; from app.db.database import DATABASE_URL; print(make_url(DATABASE_URL).password or "")' 2>>"${LOG}" || true)}"
if command -v pg_dump >/dev/null 2>&1; then
  log "Dumping PostgreSQL ${PG_DB}..."
  PGPASSWORD="${PG_PASSWORD}" pg_dump -U "${PG_USER}" -h localhost -Fc "${PG_DB}" -f "${DEST}/rmp_db.dump" 2>>"${LOG}" \
    || fail postgres "pg_dump failed (check credentials)"
else
  fail postgres "pg_dump not found"
fi

# Postgres: Temporal's own databases (the live workflow store), through the postgres superuser's peer login.
for db in ${TEMPORAL_DBS}; do
  log "Dumping Temporal database ${db}..."
  if sudo -n -u postgres pg_dump -Fc "${db}" > "${DEST}/${db}.dump.partial" 2>>"${LOG}" && [[ -s "${DEST}/${db}.dump.partial" ]]; then
    mv "${DEST}/${db}.dump.partial" "${DEST}/${db}.dump"
  else
    rm -f "${DEST}/${db}.dump.partial"
    fail temporal "pg_dump of ${db} failed"
  fi
done

# Qdrant. In server mode a full snapshot through Qdrant's API is consistent, unlike a tar of live storage.
QDRANT_CFG="$("${PY}" -c '
from app.config import get_vector_memory_config
c = get_vector_memory_config()
mode = (c.get("qdrant_mode") or "embedded").strip().lower()
server = mode == "server" or bool(c.get("qdrant_host"))
print("server" if server else "embedded")
print("http://%s:%d" % (c.get("qdrant_host") or "127.0.0.1", int(c.get("qdrant_port") or 6333)) if server else c.get("qdrant_path") or "")
' 2>>"${LOG}" || true)"
QDRANT_MODE="$(sed -n 1p <<<"${QDRANT_CFG}")"
QDRANT_WHERE="$(sed -n 2p <<<"${QDRANT_CFG}")"

qdrant_snapshot() {
  local url="$1" part="${DEST}/qdrant-full.snapshot.partial" body stale got
  local -a meta
  QDRANT_URL="${url}"
  # After a boot the timer fires a missed run early: give Qdrant a minute to come up.
  curl -fsS -m 10 --retry 12 --retry-delay 5 --retry-all-errors -o /dev/null "${url}/readyz" 2>>"${LOG}" \
    || { fail qdrant "Qdrant at ${url} is not ready"; return 0; }
  # Full snapshots on the server come only from this script; any found now were left by a run that was killed.
  for stale in $(curl -fsS -m 30 "${url}/snapshots" 2>>"${LOG}" \
                 | "${PY}" -c 'import json, sys; print("\n".join(s["name"] for s in json.load(sys.stdin).get("result") or []))' 2>>"${LOG}"); do
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
  got="$(stat -c %s "${part}")"
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

# Artifacts (full tar)
ART="${DATA_DIR}/artifacts"
if [[ -d "${ART}" ]]; then
  log "Archiving artifacts..."
  tar -czf "${DEST}/artifacts.tar.gz" -C "${DATA_DIR}" artifacts 2>>"${LOG}" || fail artifacts "tar failed"
fi

# Legacy Temporal SQLite file, from before Temporal moved to Postgres (SQLite backup API, not a hot cp)
TEMPORAL_DB="${DATA_DIR}/temporal.db"
if [[ -f "${TEMPORAL_DB}" ]]; then
  log "Backing up the legacy Temporal SQLite file..."
  if "${PY}" - <<PY
import sqlite3
src = sqlite3.connect("${TEMPORAL_DB}", timeout=10)
dst = sqlite3.connect("${DEST}/temporal.db")
try:
    src.backup(dst)
    print("temporal sqlite backup ok")
finally:
    dst.close()
    src.close()
PY
  then
    :
  else
    log "WARN: sqlite backup API failed; copying files"
    cp -a "${TEMPORAL_DB}" "${DEST}/temporal.db" || fail temporal-sqlite "copy failed"
    if [[ -f "${TEMPORAL_DB}-wal" ]]; then cp -a "${TEMPORAL_DB}-wal" "${DEST}/temporal.db-wal" || fail temporal-sqlite "wal copy failed"; fi
    if [[ -f "${TEMPORAL_DB}-shm" ]]; then cp -a "${TEMPORAL_DB}-shm" "${DEST}/temporal.db-shm" || fail temporal-sqlite "shm copy failed"; fi
  fi
fi

# OpenClaw's agent and state stores (sessions, transcripts, auth, cron): SQLite backup API
log "Backing up OpenClaw stores..."
"${PY}" "${RMP_ROOT}/ops/backup_openclaw_state.py" backup --dest "${DEST}/openclaw-state" \
  >>"${LOG}" 2>&1 || fail openclaw-state "the OpenClaw store backup failed"

# Config snapshots (absent files are skipped)
cp -a "${SETTINGS}" "${DEST}/settings.json" 2>/dev/null || true
cp -a "${OPENCLAW_HOME}/openclaw.json" "${DEST}/openclaw.json" 2>/dev/null || true
cp -a "${OPENCLAW_HOME}/cron/jobs.json" "${DEST}/cron_jobs.json" 2>/dev/null || true

# Manifest
failures_json="$(printf '%s\n' "${FAILURES[@]+"${FAILURES[@]}"}" | "${PY}" -c 'import json, sys; print(json.dumps([l for l in sys.stdin.read().splitlines() if l]))')"
cat > "${DEST}/manifest.json" <<EOF
{
  "timestamp": "${STAMP}",
  "components": ["postgres", "temporal", "qdrant", "artifacts", "openclaw-state", "settings"],
  "failures": ${failures_json},
  "host": "$(hostname)"
}
EOF

du -sh "${DEST}" | tee -a "${LOG}"
if (( ${#FAILURES[@]} )); then
  # A failed run prunes nothing: night after night of failures must never push out the last good backup.
  log "Backup INCOMPLETE: ${FAILURES[*]} failed (${DEST}); no older backup was pruned"
  exit 1
fi

# Retention: keep the newest ${KEEP} dated backups. Only directories named like a backup stamp are pruned, newest
# first by name, so anything else kept here (an OpenClaw rollback backup, a state copy) is never deleted by age.
log "Pruning old backups (keep ${KEEP})..."
shopt -s nullglob
dated=("${BACKUP_ROOT}"/20[0-9][0-9][01][0-9][0-3][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z/)
shopt -u nullglob
if (( ${#dated[@]} > KEEP )); then
  printf '%s\n' "${dated[@]}" | sort -r | tail -n +$((KEEP + 1)) | xargs -r -d '\n' rm -rf
fi
log "Backup complete: ${DEST}"
