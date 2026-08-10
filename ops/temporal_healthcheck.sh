#!/usr/bin/env bash
# Temporal watchdog entrypoint — exit non-zero when unhealthy; restarts on --recover.
set -euo pipefail
RMP_ROOT="/root/.openclaw/rmp"
exec env PYTHONPATH="${RMP_ROOT}" "${RMP_ROOT}/venv/bin/python" "${RMP_ROOT}/ops/temporal_healthcheck.py" "$@"
