#!/usr/bin/env bash
# Start Qdrant server for RMP vector memory.
set -euo pipefail

RMP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="${RMP_ROOT}/docker-compose.qdrant.yml"
STORAGE="${RMP_ROOT}/data/qdrant-server"
IMAGE="qdrant/qdrant:v1.17.0"
CONTAINER="rmp-qdrant"

mkdir -p "${STORAGE}"

start_with_compose() {
  cd "${RMP_ROOT}"
  if docker compose version >/dev/null 2>&1; then
    docker compose -f "${COMPOSE_FILE}" up -d
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose -f "${COMPOSE_FILE}" up -d
  else
    return 1
  fi
}

start_with_docker_run() {
  if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
    docker start "${CONTAINER}" >/dev/null
  else
    docker run -d \
      --name "${CONTAINER}" \
      --restart unless-stopped \
      -p 127.0.0.1:6333:6333 \
      -p 127.0.0.1:6334:6334 \
      -v "${STORAGE}:/qdrant/storage" \
      -e QDRANT__SERVICE__GRPC_PORT=6334 \
      "${IMAGE}" >/dev/null
  fi
}

if start_with_compose 2>/dev/null; then
  :
else
  start_with_docker_run
fi

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
