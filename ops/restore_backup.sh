#!/usr/bin/env bash
# Restore RMP from a backup directory made by ops/backup.sh: the RMP and Temporal databases, Qdrant, artifacts and
# settings. Runbook: docs/runbooks/backup-restore.md. Exits 1, naming what was not restored, if any part fails.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 /root/.openclaw/rmp/data/backups/YYYYMMDDTHHMMSSZ"
  exit 1
fi

SRC="$1"
RMP_ROOT="/root/.openclaw/rmp"
RMP_DATA="${RMP_ROOT}/data"
PY="${RMP_ROOT}/venv/bin/python"
PG_USER="${PGUSER:-rmp}"
# The rmp role's password lives only in DATABASE_URL (environment or /etc/rmp/rmp.env).
PG_PASSWORD="${PGPASSWORD:-$(cd "${RMP_ROOT}" && "${PY}" -c 'from sqlalchemy.engine import make_url; from app.db.database import DATABASE_URL; print(make_url(DATABASE_URL).password or "")')}"
PG_DB="${PGDATABASE:-rmp_db}"
FAILED=()

if [[ ! -d "$SRC" ]]; then
  echo "Backup directory not found: $SRC"
  exit 1
fi
if [[ -f "${SRC}/manifest.json" ]]; then
  failures="$("${PY}" -c 'import json, sys; print(" ".join(json.load(open(sys.argv[1])).get("failures") or []))' "${SRC}/manifest.json")"
  [[ -z "${failures}" ]] || echo "WARNING: this backup recorded failures when it was made: ${failures}"
fi

echo "=== RMP Restore from $SRC ==="
read -r -p "This will overwrite live data. Continue? [y/N] " confirm
[[ "$confirm" == "y" || "$confirm" == "Y" ]] || exit 0

systemctl stop rmp-api rmp-worker temporal || true

if [[ -f "${SRC}/rmp_db.dump" && -s "${SRC}/rmp_db.dump" ]]; then
  echo "Restoring Postgres ${PG_DB}..."
  if ! { PGPASSWORD="$PG_PASSWORD" dropdb -U "$PG_USER" -h localhost --if-exists "$PG_DB" \
         && PGPASSWORD="$PG_PASSWORD" createdb -U "$PG_USER" -h localhost "$PG_DB" \
         && PGPASSWORD="$PG_PASSWORD" pg_restore -U "$PG_USER" -h localhost -d "$PG_DB" "${SRC}/rmp_db.dump"; }; then
    FAILED+=("postgres")
  fi
fi

# Temporal's own databases (Temporal is stopped above), through the postgres superuser's peer login.
for db in temporal temporal_visibility; do
  if [[ -s "${SRC}/${db}.dump" ]]; then
    echo "Restoring Temporal database ${db}..."
    sudo -n -u postgres pg_restore --clean --if-exists -d "${db}" < "${SRC}/${db}.dump" || FAILED+=("temporal:${db}")
  fi
done

# Qdrant: a full snapshot restores collection by collection into the running server.
if [[ -f "${SRC}/qdrant-full.snapshot" ]]; then
  echo "Restoring Qdrant collections..."
  bash "${RMP_ROOT}/ops/restore_qdrant_snapshot.sh" "${SRC}/qdrant-full.snapshot" || FAILED+=("qdrant")
elif [[ -f "${SRC}/qdrant.tar.gz" ]]; then
  # Backups made before 2026-10-08 (or in embedded mode) carry a tar of the embedded storage directory.
  echo "Restoring embedded Qdrant data..."
  { rm -rf "${RMP_DATA}/qdrant" && tar -xzf "${SRC}/qdrant.tar.gz" -C "${RMP_DATA}"; } || FAILED+=("qdrant")
fi

if [[ -f "${SRC}/artifacts.tar.gz" ]]; then
  echo "Restoring artifacts..."
  { rm -rf "${RMP_DATA}/artifacts" && tar -xzf "${SRC}/artifacts.tar.gz" -C "${RMP_DATA}"; } || FAILED+=("artifacts")
fi

if [[ -f "${SRC}/settings.json" ]]; then
  cp -a "${SRC}/settings.json" "${RMP_ROOT}/settings.json" || FAILED+=("settings")
fi

systemctl restart temporal rmp-api rmp-worker openclaw-gateway

if (( ${#FAILED[@]} )); then
  echo "Restore INCOMPLETE: not restored: ${FAILED[*]}" >&2
  exit 1
fi
echo "Restore complete."
