#!/usr/bin/env bash
set -euo pipefail

RMP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="${RMP_ROOT}/docker-compose.qdrant.yml"
CONTAINER="rmp-qdrant"

cd "${RMP_ROOT}"

if docker compose version >/dev/null 2>&1; then
  docker compose -f "${COMPOSE_FILE}" down 2>/dev/null && exit 0
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose -f "${COMPOSE_FILE}" down 2>/dev/null && exit 0
fi

if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
  docker stop "${CONTAINER}" >/dev/null 2>&1 || true
fi
