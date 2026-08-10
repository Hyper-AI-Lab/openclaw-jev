#!/usr/bin/env bash
# One-time migration: embedded Qdrant folder → Qdrant server (Docker).
# Stops rmp-worker briefly so the embedded .lock is released.
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
VENV="${RMP_ROOT}/venv/bin/python"
EMBEDDED="${RMP_ROOT}/data/qdrant"
ARCHIVE="${RMP_ROOT}/data/qdrant-embedded-archive"

echo "=== RMP Qdrant embedded → server migration ==="

bash "${RMP_ROOT}/ops/start_qdrant.sh"

if [[ ! -d "${EMBEDDED}/collection" ]]; then
  echo "No embedded Qdrant data at ${EMBEDDED}; skipping point copy."
  exit 0
fi

echo "Stopping rmp-worker to release embedded Qdrant lock..."
systemctl stop rmp-worker.service

"${VENV}" "${RMP_ROOT}/ops/migrate_qdrant_embedded_to_server.py" \
  --embedded-path "${EMBEDDED}" \
  --host 127.0.0.1 \
  --port 6333 \
  --collection rmp_memories

if [[ ! -d "${ARCHIVE}" ]]; then
  echo "Archiving embedded storage to ${ARCHIVE}..."
  mv "${EMBEDDED}" "${ARCHIVE}"
  mkdir -p "${EMBEDDED}"
  echo "Embedded store archived (not used in server mode)."
fi

echo "Restarting RMP services..."
systemctl start rmp-worker.service
systemctl restart rmp-api.service

echo "Migration complete. Verify: curl -s http://127.0.0.1:8000/memory/vector/status | jq ."
