#!/usr/bin/env bash
# RMP production backup — Postgres, Qdrant, artifacts, settings, OpenClaw cron snapshot.
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
BACKUP_ROOT="${RMP_ROOT}/data/backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${BACKUP_ROOT}/${STAMP}"
PG_DB="${PGDATABASE:-rmp_db}"
PG_USER="${PGUSER:-rmp}"
# The rmp role's password lives only in DATABASE_URL (environment or /etc/rmp/rmp.env). Without it pg_dump
# fails and is logged below, and the rest of the backup still runs.
PG_PASSWORD="${PGPASSWORD:-$(cd "${RMP_ROOT}" && ./venv/bin/python -c 'from sqlalchemy.engine import make_url; from app.db.database import DATABASE_URL; print(make_url(DATABASE_URL).password or "")' 2>/dev/null || true)}"
LOG="${DEST}/backup.log"

mkdir -p "${DEST}"

log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "${LOG}"; }

log "Starting RMP backup → ${DEST}"

# Postgres
if command -v pg_dump >/dev/null 2>&1; then
  log "Dumping PostgreSQL ${PG_DB}..."
  PGPASSWORD="${PG_PASSWORD}" pg_dump -U "${PG_USER}" -h localhost -Fc "${PG_DB}" -f "${DEST}/rmp_db.dump" 2>>"${LOG}" || {
    log "WARN: pg_dump failed (check credentials)"
  }
else
  log "WARN: pg_dump not found"
fi

# Qdrant vector storage (embedded path or server bind mount)
QDRANT_DIR="$("${RMP_ROOT}/venv/bin/python" -c "
from app.config import get_vector_memory_config
c = get_vector_memory_config()
mode = (c.get('qdrant_mode') or 'embedded').strip().lower()
if mode == 'server' or c.get('qdrant_host'):
    print('${RMP_ROOT}/data/qdrant-server')
else:
    print(c.get('qdrant_path', '${RMP_ROOT}/data/qdrant'))
" 2>/dev/null || echo "${RMP_ROOT}/data/qdrant")"
if [[ -d "${QDRANT_DIR}" ]]; then
  log "Archiving Qdrant data from ${QDRANT_DIR}..."
  tar -czf "${DEST}/qdrant.tar.gz" -C "$(dirname "${QDRANT_DIR}")" "$(basename "${QDRANT_DIR}")" 2>>"${LOG}" || true
fi

# Artifacts (incremental-friendly: full tar for v1)
ART="${RMP_ROOT}/data/artifacts"
if [[ -d "${ART}" ]]; then
  log "Archiving artifacts..."
  tar -czf "${DEST}/artifacts.tar.gz" -C "${RMP_ROOT}/data" artifacts 2>>"${LOG}" || true
fi

# Temporal persistent DB if present (SQLite backup API, not a hot cp)
TEMPORAL_DB="${RMP_ROOT}/data/temporal.db"
if [[ -f "${TEMPORAL_DB}" ]]; then
  log "Backing up Temporal DB..."
  if PYTHONPATH="${RMP_ROOT}" "${RMP_ROOT}/venv/bin/python" - <<PY
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
    cp -a "${TEMPORAL_DB}" "${DEST}/temporal.db"
    [[ -f "${TEMPORAL_DB}-wal" ]] && cp -a "${TEMPORAL_DB}-wal" "${DEST}/temporal.db-wal"
    [[ -f "${TEMPORAL_DB}-shm" ]] && cp -a "${TEMPORAL_DB}-shm" "${DEST}/temporal.db-shm"
  fi
fi

# OpenClaw's agent and state stores (sessions, transcripts, auth, cron): SQLite backup API
log "Backing up OpenClaw stores..."
"${RMP_ROOT}/venv/bin/python" "${RMP_ROOT}/ops/backup_openclaw_state.py" backup --dest "${DEST}/openclaw-state" \
  >>"${LOG}" 2>&1 || log "WARN: OpenClaw store backup failed"

# Config snapshots
cp -a "${RMP_ROOT}/settings.json" "${DEST}/settings.json" 2>/dev/null || true
cp -a /root/.openclaw/openclaw.json "${DEST}/openclaw.json" 2>/dev/null || true
cp -a /root/.openclaw/cron/jobs.json "${DEST}/cron_jobs.json" 2>/dev/null || true

# Manifest
cat > "${DEST}/manifest.json" <<EOF
{
  "timestamp": "${STAMP}",
  "components": ["postgres", "qdrant", "artifacts", "temporal", "openclaw-state", "settings"],
  "host": "$(hostname)"
}
EOF

# Retention: keep last 14 daily-ish backups
log "Pruning old backups (keep 14)..."
ls -1dt "${BACKUP_ROOT}"/*/ 2>/dev/null | tail -n +15 | xargs -r rm -rf

log "Backup complete: ${DEST}"
du -sh "${DEST}" | tee -a "${LOG}"
