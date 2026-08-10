#!/usr/bin/env bash
# Full Temporal recovery: stop workers, purge stale workflows, vacuum SQLite, restart stack.
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
VENV="${RMP_ROOT}/venv/bin/python"
DB="${RMP_ROOT}/data/temporal.db"
BACKUP_DIR="${RMP_ROOT}/data/backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

echo "=== Temporal full recovery (${STAMP}) ==="

mkdir -p "${BACKUP_DIR}"
if [[ -f "${DB}" ]]; then
  cp -a "${DB}" "${BACKUP_DIR}/temporal-${STAMP}.db"
  echo "Backed up temporal.db"
fi

echo "[1/6] Stopping RMP API + worker..."
systemctl stop rmp-worker.service rmp-api.service || true

echo "[2/6] Purging stale Temporal workflows..."
cd "${RMP_ROOT}"
PYTHONPATH="${RMP_ROOT}" "${VENV}" ops/temporal_purge_running.py --force-recovery || true

echo "[3/6] Restarting Temporal dev server..."
systemctl restart temporal-dev.service
sleep 5

if [[ -f "${DB}" ]]; then
  echo "[4/6] SQLite maintenance (WAL checkpoint + VACUUM)..."
  systemctl stop temporal-dev.service
  PYTHONPATH="${RMP_ROOT}" "${VENV}" -c "
import sqlite3
conn = sqlite3.connect('${DB}')
conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
conn.execute('VACUUM')
conn.close()
print('SQLite VACUUM ok')
"
  systemctl start temporal-dev.service
  sleep 5
else
  echo "[4/6] Skipping VACUUM (db missing)"
fi

echo "[5/6] Temporal health probe..."
"${VENV}" ops/temporal_healthcheck.py --recover || {
  echo "Temporal health probe failed after recovery" >&2
  exit 1
}

echo "[6/6] Starting RMP + gateway..."
systemctl restart rmp-api.service rmp-worker.service openclaw-gateway.service
sleep 5

curl -sf -H "X-RMP-API-Key: $(python3 -c "import json; print(json.load(open('${RMP_ROOT}/settings.json'))['api_key'])")" \
  http://127.0.0.1:8000/health | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('status')=='ok', d; print('health OK')"

echo "=== Temporal recovery complete ==="
