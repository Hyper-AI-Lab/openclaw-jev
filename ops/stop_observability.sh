#!/usr/bin/env bash
# Stop RMP observability stack (OTel Collector + Phoenix).
set -euo pipefail

RMP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="${RMP_ROOT}/docker-compose.observability.yml"

cd "${RMP_ROOT}"

if docker compose version >/dev/null 2>&1; then
  docker compose -f "${COMPOSE_FILE}" down
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose -f "${COMPOSE_FILE}" down
else
  echo "Docker Compose not available" >&2
  exit 1
fi

echo "Observability stack stopped."
