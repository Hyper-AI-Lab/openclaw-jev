#!/usr/bin/env bash
# RMP production backup — Postgres, Qdrant, artifacts, settings, OpenClaw cron snapshot.
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
BACKUP_ROOT="${RMP_ROOT}/data/backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${BACKUP_ROOT}/${STAMP}"
PG_DB="${PGDATABASE:-rmp_db}"
PG_USER="${PGUSER:-rmp}"
PG_PASSWORD="${PGPASSWORD:-rmp_password}"
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

# Temporal persistent DB if present
TEMPORAL_DB="${RMP_ROOT}/data/temporal.db"
if [[ -f "${TEMPORAL_DB}" ]]; then
  log "Copying Temporal DB..."
  cp -a "${TEMPORAL_DB}" "${DEST}/temporal.db"
fi

# Config snapshots
cp -a "${RMP_ROOT}/settings.json" "${DEST}/settings.json" 2>/dev/null || true
cp -a /root/.openclaw/openclaw.json "${DEST}/openclaw.json" 2>/dev/null || true
cp -a /root/.openclaw/cron/jobs.json "${DEST}/cron_jobs.json" 2>/dev/null || true

# Manifest
cat > "${DEST}/manifest.json" <<EOF
{
  "timestamp": "${STAMP}",
  "components": ["postgres", "qdrant", "artifacts", "temporal", "settings"],
  "host": "$(hostname)"
}
EOF

# Retention: keep last 14 daily-ish backups
log "Pruning old backups (keep 14)..."
ls -1dt "${BACKUP_ROOT}"/*/ 2>/dev/null | tail -n +15 | xargs -r rm -rf

log "Backup complete: ${DEST}"
du -sh "${DEST}" | tee -a "${LOG}"
