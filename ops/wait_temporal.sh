#!/usr/bin/env bash
# Wait until Temporal gRPC on localhost:7233 accepts a client.
set -euo pipefail
RMP_ROOT="/root/.openclaw/rmp"
VENV="${RMP_ROOT}/venv/bin/python"
for _ in $(seq 1 90); do
  if PYTHONPATH="${RMP_ROOT}" "${VENV}" "${RMP_ROOT}/ops/temporal_healthcheck.py" >/dev/null 2>&1; then
    exit 0
  fi
  sleep 1
done
echo "wait_temporal: gRPC not ready after 90s" >&2
exit 1
