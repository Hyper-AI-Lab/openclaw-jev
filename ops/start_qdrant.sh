#!/usr/bin/env bash
# Start Qdrant server for RMP vector memory: the one container rmp-qdrant, which rmp-qdrant.service starts through this
# script and stops through ops/stop_qdrant.sh.
#
# Every start stops a running rmp-qdrant gracefully, removes it and recreates it from the flags below, so a flag change
# takes effect at the next `systemctl restart rmp-qdrant`. The data lives in the bind mount ${DATA_DIR}/qdrant-server
# and survives the recreate.
#
# Whenever a canary sentinel run finds a canary issue, it restarts every inactive unit of its SERVICE_UNITS,
# rmp-qdrant among them (app/production/canary_sentinel.py:32-38,481-487,665-668; every 30 min). So a procedure that
# keeps Qdrant stopped stops the sentinel's timer first (docs/runbooks/backup-restore.md, "Whole store from scratch").
set -euo pipefail

RMP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${RMP_DATA_DIR:-${RMP_ROOT}/data}"
STORAGE="${DATA_DIR}/qdrant-server"
IMAGE="qdrant/qdrant:v1.17.0"
CONTAINER="rmp-qdrant"

mkdir -p "${STORAGE}"

# docker stop, never docker rm -f: Docker's unless-stopped policy starts the container itself when the daemon starts,
# so it may still be loading its collections or replaying its WAL, and rm -f would SIGKILL it. Stopping a stopped
# container does nothing.
if docker container inspect "${CONTAINER}" >/dev/null; then
  docker stop "${CONTAINER}" >/dev/null
  docker rm "${CONTAINER}" >/dev/null
fi

docker run -d --name "${CONTAINER}" --restart unless-stopped \
  -p 127.0.0.1:6333:6333 -p 127.0.0.1:6334:6334 \
  -v "${STORAGE}:/qdrant/storage" \
  -e QDRANT__SERVICE__GRPC_PORT=6334 \
  "${IMAGE}" >/dev/null

for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:6333/readyz" >/dev/null 2>&1; then
    echo "Qdrant ready at http://127.0.0.1:6333"
    exit 0
  fi
  sleep 1
done

echo "Qdrant failed to become ready within 30s" >&2
docker logs --tail 30 "${CONTAINER}" >&2 || true
exit 1
