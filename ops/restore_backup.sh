#!/usr/bin/env bash
# Restore RMP from a backup directory made by ops/backup.sh: the RMP and Temporal databases, Qdrant, artifacts and
# settings. Runbook: docs/runbooks/backup-restore.md. Exits 1, naming what was not restored, if any part fails.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 /root/.openclaw/rmp/data/backups/YYYYMMDDTHHMMSSZ"
  exit 1
fi

SRC="$1"
# The names and defaults of ops/backup.sh. Production sets none of them; the tests point them at a tmp tree.
RMP_ROOT="${RMP_ROOT:-/root/.openclaw/rmp}"
RMP_DATA="${RMP_DATA_DIR:-${RMP_ROOT}/data}"
PY="${RMP_PYTHON:-${RMP_ROOT}/venv/bin/python}"
SETTINGS="${RMP_SETTINGS_PATH:-${RMP_ROOT}/settings.json}"
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

# RMP's ledger, then Temporal's own databases (Temporal is stopped above). Each is dropped and recreated as an exact
# copy of its archive through the postgres superuser: the rmp role cannot create a database. A failure is named as in
# the backup's manifest.
declare -A PART=([rmp_db]=postgres [temporal]=temporal:temporal [temporal_visibility]=temporal:temporal_visibility)
for db in rmp_db temporal temporal_visibility; do
  if [[ -s "${SRC}/${db}.dump" ]]; then
    echo "Restoring the ${db} database..."
    bash "${RMP_ROOT}/ops/restore_pg_database.sh" "${SRC}/${db}.dump" || FAILED+=("${PART[${db}]}")
  fi
done

# Qdrant: a full snapshot restores collection by collection into the running server.
if [[ -f "${SRC}/qdrant-full.snapshot" ]]; then
  echo "Restoring Qdrant collections..."
  bash "${RMP_ROOT}/ops/restore_qdrant_snapshot.sh" "${SRC}/qdrant-full.snapshot" || FAILED+=("qdrant")
elif [[ -f "${SRC}/qdrant.tar.gz" ]]; then
  # Backups made before 2026-10-08 carry a tar of the legacy embedded directory, which was empty in server mode:
  # it does not restore the Qdrant server (data/qdrant-server). Only embedded-mode backups hold real data in it.
  echo "WARNING: qdrant.tar.gz restores the embedded store only; it does not restore a Qdrant server."
  echo "Restoring embedded Qdrant data..."
  { rm -rf "${RMP_DATA}/qdrant" && tar -xzf "${SRC}/qdrant.tar.gz" -C "${RMP_DATA}"; } || FAILED+=("qdrant")
fi

if [[ -f "${SRC}/artifacts.tar.gz" ]]; then
  echo "Restoring artifacts..."
  { rm -rf "${RMP_DATA}/artifacts" && tar -xzf "${SRC}/artifacts.tar.gz" -C "${RMP_DATA}"; } || FAILED+=("artifacts")
fi

if [[ -f "${SRC}/settings.json" ]]; then
  cp -a "${SRC}/settings.json" "${SETTINGS}" || FAILED+=("settings")
fi

systemctl restart temporal rmp-api rmp-worker openclaw-gateway

if (( ${#FAILED[@]} )); then
  echo "Restore INCOMPLETE: not restored: ${FAILED[*]}" >&2
  exit 1
fi
echo "Restore complete."
