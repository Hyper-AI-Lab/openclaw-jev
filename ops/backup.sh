#!/usr/bin/env bash
# RMP production backup: Postgres, a Qdrant snapshot, artifacts, the OpenClaw stores and config snapshots.
# Every component runs even when one fails; the failures are listed in manifest.json and the script exits 1,
# so rmp-backup.service shows the run as failed instead of passing with a broken backup.
set -euo pipefail

RMP_ROOT="${RMP_ROOT:-/root/.openclaw/rmp}"
BACKUP_ROOT="${RMP_BACKUP_ROOT:-${RMP_ROOT}/data/backups}"
PY="${RMP_PYTHON:-${RMP_ROOT}/venv/bin/python}"
OPENCLAW_HOME="${OPENCLAW_HOME:-/root/.openclaw}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${BACKUP_ROOT}/${STAMP}"
PG_DB="${PGDATABASE:-rmp_db}"
PG_USER="${PGUSER:-rmp}"
LOG="${DEST}/backup.log"
FAILURES=()

# The unit starts this script without a working directory: run from the checkout so `app` imports resolve.
cd "${RMP_ROOT}"
mkdir -p "${DEST}"

log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "${LOG}"; }
fail() { FAILURES+=("$1"); log "FAIL: $1: $2"; }

log "Starting RMP backup → ${DEST}"

# Postgres. The rmp role's password lives only in DATABASE_URL (environment or /etc/rmp/rmp.env).
PG_PASSWORD="${PGPASSWORD:-$("${PY}" -c 'from sqlalchemy.engine import make_url; from app.db.database import DATABASE_URL; print(make_url(DATABASE_URL).password or "")' 2>>"${LOG}" || true)}"
if command -v pg_dump >/dev/null 2>&1; then
  log "Dumping PostgreSQL ${PG_DB}..."
  PGPASSWORD="${PG_PASSWORD}" pg_dump -U "${PG_USER}" -h localhost -Fc "${PG_DB}" -f "${DEST}/rmp_db.dump" 2>>"${LOG}" \
    || fail postgres "pg_dump failed (check credentials)"
else
  fail postgres "pg_dump not found"
fi

# Qdrant. In server mode a full snapshot through Qdrant's API is consistent, unlike a tar of live storage;
# it is downloaded here and then removed from the server, which keeps its own copy in the container.
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
if [[ "${QDRANT_MODE}" == "server" && -n "${QDRANT_WHERE}" ]]; then
  log "Snapshotting Qdrant at ${QDRANT_WHERE}..."
  snap=""
  if snap="$(curl -fsS -m 600 -X POST "${QDRANT_WHERE}/snapshots?wait=true" 2>>"${LOG}" \
              | "${PY}" -c 'import json, sys; print(json.load(sys.stdin)["result"]["name"])' 2>>"${LOG}")" \
     && [[ -n "${snap}" ]] \
     && curl -fsS -m 900 -o "${DEST}/qdrant-full.snapshot" "${QDRANT_WHERE}/snapshots/${snap}" 2>>"${LOG}" \
     && (( $(tar -tf "${DEST}/qdrant-full.snapshot" 2>>"${LOG}" | wc -l) > 0 )); then
    log "Qdrant snapshot ${snap}: $(du -h "${DEST}/qdrant-full.snapshot" | cut -f1)"
  else
    fail qdrant "the full snapshot was not created, downloaded or readable"
  fi
  if [[ -n "${snap}" ]]; then
    curl -fsS -m 60 -X DELETE "${QDRANT_WHERE}/snapshots/${snap}" >/dev/null 2>>"${LOG}" \
      || log "WARN: could not delete snapshot ${snap} from the Qdrant server"
  fi
elif [[ "${QDRANT_MODE}" == "embedded" && -d "${QDRANT_WHERE}" ]]; then
  log "Archiving embedded Qdrant data from ${QDRANT_WHERE}..."
  tar -czf "${DEST}/qdrant.tar.gz" -C "$(dirname "${QDRANT_WHERE}")" "$(basename "${QDRANT_WHERE}")" 2>>"${LOG}" \
    || fail qdrant "tar of ${QDRANT_WHERE} failed"
else
  fail qdrant "could not resolve Qdrant from settings (mode '${QDRANT_MODE}', '${QDRANT_WHERE}')"
fi

# Artifacts (full tar)
ART="${RMP_ROOT}/data/artifacts"
if [[ -d "${ART}" ]]; then
  log "Archiving artifacts..."
  tar -czf "${DEST}/artifacts.tar.gz" -C "${RMP_ROOT}/data" artifacts 2>>"${LOG}" || fail artifacts "tar failed"
fi

# Temporal persistent DB if present (SQLite backup API, not a hot cp)
TEMPORAL_DB="${RMP_ROOT}/data/temporal.db"
if [[ -f "${TEMPORAL_DB}" ]]; then
  log "Backing up Temporal DB..."
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
    [[ -f "${TEMPORAL_DB}-wal" ]] && cp -a "${TEMPORAL_DB}-wal" "${DEST}/temporal.db-wal"
    [[ -f "${TEMPORAL_DB}-shm" ]] && cp -a "${TEMPORAL_DB}-shm" "${DEST}/temporal.db-shm"
  fi
fi

# OpenClaw's agent and state stores (sessions, transcripts, auth, cron): SQLite backup API
log "Backing up OpenClaw stores..."
"${PY}" "${RMP_ROOT}/ops/backup_openclaw_state.py" backup --dest "${DEST}/openclaw-state" \
  >>"${LOG}" 2>&1 || fail openclaw-state "the OpenClaw store backup failed"

# Config snapshots (absent files are skipped)
cp -a "${RMP_ROOT}/settings.json" "${DEST}/settings.json" 2>/dev/null || true
cp -a "${OPENCLAW_HOME}/openclaw.json" "${DEST}/openclaw.json" 2>/dev/null || true
cp -a "${OPENCLAW_HOME}/cron/jobs.json" "${DEST}/cron_jobs.json" 2>/dev/null || true

# Manifest
failures_json="$(printf '%s\n' "${FAILURES[@]+"${FAILURES[@]}"}" | "${PY}" -c 'import json, sys; print(json.dumps([l for l in sys.stdin.read().splitlines() if l]))')"
cat > "${DEST}/manifest.json" <<EOF
{
  "timestamp": "${STAMP}",
  "components": ["postgres", "qdrant", "artifacts", "temporal", "openclaw-state", "settings"],
  "failures": ${failures_json},
  "host": "$(hostname)"
}
EOF

# Retention: keep the last 14 dated backups. Only directories named like a backup stamp are pruned, so
# anything else kept here (an OpenClaw rollback backup, a state copy) is never deleted by age.
log "Pruning old backups (keep 14)..."
ls -1dt "${BACKUP_ROOT}"/20[0-9][0-9][01][0-9][0-3][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z/ 2>/dev/null | tail -n +15 | xargs -r rm -rf

du -sh "${DEST}" | tee -a "${LOG}"
if (( ${#FAILURES[@]} )); then
  log "Backup INCOMPLETE: ${FAILURES[*]} failed (${DEST})"
  exit 1
fi
log "Backup complete: ${DEST}"
