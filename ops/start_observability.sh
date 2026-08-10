#!/usr/bin/env bash
# Start RMP observability stack (OTel Collector + Phoenix).
# After start, set in settings.json:
#   "telemetry": { "otlp_endpoint": "http://127.0.0.1:4318/v1/traces", ... }
# Then restart rmp-api and rmp-worker.
set -euo pipefail

RMP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="${RMP_ROOT}/docker-compose.observability.yml"

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "Missing ${COMPOSE_FILE}" >&2
  exit 1
fi

cd "${RMP_ROOT}"

if docker compose version >/dev/null 2>&1; then
  echo "Using: docker compose"
  docker compose -f "${COMPOSE_FILE}" up -d
elif command -v docker-compose >/dev/null 2>&1; then
  echo "Using: docker-compose"
  docker-compose -f "${COMPOSE_FILE}" up -d
else
  echo "Neither 'docker compose' nor 'docker-compose' is available." >&2
  echo "Install Docker Compose, then re-run this script." >&2
  exit 1
fi

echo ""
echo "Observability stack started."
echo "  OTLP HTTP:  http://127.0.0.1:4318/v1/traces"
echo "  Phoenix UI: http://127.0.0.1:6006"
echo ""
echo "Update ${RMP_ROOT}/settings.json:"
echo '  "telemetry": { "enabled": true, "otlp_endpoint": "http://127.0.0.1:4318/v1/traces" }'
echo "Then: systemctl restart rmp-api rmp-worker  # or your process manager"
