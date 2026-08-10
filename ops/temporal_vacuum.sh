#!/usr/bin/env bash
# Weekly SQLite maintenance for Temporal dev server (reduces lock/contention errors).
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
DB="${RMP_ROOT}/data/temporal.db"
BACKUP_DIR="${RMP_ROOT}/data/backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

[[ -f "${DB}" ]] || exit 0

VENV="${RMP_ROOT}/venv/bin/python"

mkdir -p "${BACKUP_DIR}"
cp -a "${DB}" "${BACKUP_DIR}/temporal-pre-vacuum-${STAMP}.db"

systemctl stop rmp-worker.service rmp-api.service || true
systemctl stop temporal-dev.service
"${VENV}" -c "
import sqlite3
conn = sqlite3.connect('${DB}')
conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
conn.execute('VACUUM')
conn.close()
print('SQLite VACUUM ok')
"
systemctl start temporal-dev.service
sleep 3
systemctl start rmp-api.service rmp-worker.service

echo "Temporal VACUUM complete (${STAMP})"
