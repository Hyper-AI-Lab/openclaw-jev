#!/usr/bin/env bash
# Stop Qdrant: docker stop of the container rmp-qdrant, whose data stays in its bind mount; the next start recreates it
# (ops/start_qdrant.sh). A failed stop fails rmp-qdrant.service's stop.
set -euo pipefail

CONTAINER="rmp-qdrant"

if docker container inspect "${CONTAINER}" >/dev/null; then
  docker stop "${CONTAINER}" >/dev/null
fi
