#!/usr/bin/env bash
# Restore RMP from a backup directory (Postgres + optional Qdrant/artifacts).
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 /root/.openclaw/rmp/data/backups/YYYYMMDDTHHMMSSZ"
  exit 1
fi

SRC="$1"
RMP_ROOT="/root/.openclaw/rmp"
PG_USER="${PGUSER:-rmp}"
# The rmp role's password lives only in DATABASE_URL (environment or /etc/rmp/rmp.env).
PG_PASSWORD="${PGPASSWORD:-$(cd "${RMP_ROOT}" && ./venv/bin/python -c 'from sqlalchemy.engine import make_url; from app.db.database import DATABASE_URL; print(make_url(DATABASE_URL).password or "")')}"
PG_DB="${PGDATABASE:-rmp_db}"

if [[ ! -d "$SRC" ]]; then
  echo "Backup directory not found: $SRC"
  exit 1
fi

echo "=== RMP Restore from $SRC ==="
read -r -p "This will overwrite live data. Continue? [y/N] " confirm
[[ "$confirm" == "y" || "$confirm" == "Y" ]] || exit 0

systemctl stop rmp-api rmp-worker temporal || true

if [[ -f "${SRC}/rmp_db.dump" && -s "${SRC}/rmp_db.dump" ]]; then
  echo "Restoring Postgres..."
  PGPASSWORD="$PG_PASSWORD" dropdb -U "$PG_USER" -h localhost --if-exists "$PG_DB"
  PGPASSWORD="$PG_PASSWORD" createdb -U "$PG_USER" -h localhost "$PG_DB"
  PGPASSWORD="$PG_PASSWORD" pg_restore -U "$PG_USER" -h localhost -d "$PG_DB" "${SRC}/rmp_db.dump"
fi

RMP_DATA="/root/.openclaw/rmp/data"
if [[ -f "${SRC}/qdrant.tar.gz" ]]; then
  echo "Restoring Qdrant..."
  rm -rf "${RMP_DATA}/qdrant"
  tar -xzf "${SRC}/qdrant.tar.gz" -C "${RMP_DATA}"
fi

if [[ -f "${SRC}/artifacts.tar.gz" ]]; then
  echo "Restoring artifacts..."
  rm -rf "${RMP_DATA}/artifacts"
  tar -xzf "${SRC}/artifacts.tar.gz" -C "${RMP_DATA}"
fi

if [[ -f "${SRC}/temporal.db" ]]; then
  echo "Restoring Temporal DB (temporal-dev stopped)..."
  rm -f "${RMP_DATA}/temporal.db-wal" "${RMP_DATA}/temporal.db-shm"
  cp -a "${SRC}/temporal.db" "${RMP_DATA}/temporal.db"
  [[ -f "${SRC}/temporal.db-wal" ]] && cp -a "${SRC}/temporal.db-wal" "${RMP_DATA}/temporal.db-wal"
  [[ -f "${SRC}/temporal.db-shm" ]] && cp -a "${SRC}/temporal.db-shm" "${RMP_DATA}/temporal.db-shm"
fi

if [[ -f "${SRC}/settings.json" ]]; then
  cp -a "${SRC}/settings.json" /root/.openclaw/rmp/settings.json
fi

systemctl restart temporal rmp-api rmp-worker openclaw-gateway
echo "Restore complete."
