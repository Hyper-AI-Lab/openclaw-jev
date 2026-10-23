#!/usr/bin/env bash
# Start Qdrant server for RMP vector memory: the one container rmp-qdrant, which rmp-qdrant.service starts through this
# script and stops through ops/stop_qdrant.sh.
#
# Every start stops a running rmp-qdrant gracefully, removes it and recreates it from the flags below, so a flag change
# takes effect at the next `systemctl restart rmp-qdrant`. The data lives in the bind mount ${DATA_DIR}/qdrant-server
# and survives the recreate.
#
# Many things start rmp-qdrant. Whenever a canary sentinel run finds a canary issue, it restarts every inactive unit of
# its SERVICE_UNITS, rmp-qdrant among them (app/production/canary_sentinel.py:32-38,481-487,665-668); the sentinel runs
# from its own timer every 30 min and from the failure paths of the health and memory canaries (ops/canary.sh,
# ops/canary_slack_memory.sh). A start or restart of rmp-api or rmp-worker, which want rmp-qdrant, starts it too, as do
# a boot, `make qdrant`, `make migrate-qdrant` and a start by hand. So the procedure that keeps Qdrant stopped while it
# rebuilds the storage (docs/runbooks/backup-restore.md, "Whole store from scratch") stops every rmp-* timer first, and
# this start refuses, whatever asks for it, while that procedure's container rmp-qdrant-from-snapshot exists.
#
# Qdrant's API key is qdrant_api_key()'s (app/memory/vector.py): QDRANT_API_KEY from the environment when it is set,
# else the one in /etc/rmp/rmp.env. With one, the container requires it of every request: docker takes its value from
# this script's environment, so only the variable's name is on docker's command line, and the readiness wait sends it
# through ops/qdrant_curl.sh.
set -euo pipefail

RMP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${RMP_DATA_DIR:-${RMP_ROOT}/data}"
PY="${RMP_PYTHON:-${RMP_ROOT}/venv/bin/python}"
STORAGE="${DATA_DIR}/qdrant-server"
IMAGE="qdrant/qdrant:v1.17.0"
CONTAINER="rmp-qdrant"
ONE_OFF="rmp-qdrant-from-snapshot"

# Two Qdrants never share the storage, whatever starts rmp-qdrant while the one-off container rebuilds it.
if docker container inspect "${ONE_OFF}" >/dev/null 2>&1; then
  echo "refused: ${ONE_OFF} exists and rebuilds ${STORAGE}; start rmp-qdrant once it is removed" \
    "(docs/runbooks/backup-restore.md, \"Whole store from scratch\")" >&2
  exit 1
fi

# Read before the running container is touched: a key that cannot be read fails the start and leaves Qdrant as it is.
KEY="$(cd "${RMP_ROOT}" && "${PY}" -c 'from app.memory.vector import qdrant_api_key; print(qdrant_api_key())')" \
  || { echo "could not read Qdrant's API key; rmp-qdrant is left as it is" >&2; exit 1; }
KEY_ARGS=()
if [[ -n "${KEY}" ]]; then
  export QDRANT__SERVICE__API_KEY="${KEY}"
  KEY_ARGS=(-e QDRANT__SERVICE__API_KEY)
fi

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
  -e QDRANT__SERVICE__GRPC_PORT=6334 ${KEY_ARGS[@]+"${KEY_ARGS[@]}"} \
  "${IMAGE}" >/dev/null

for _ in $(seq 1 30); do
  if bash "${RMP_ROOT}/ops/qdrant_curl.sh" -sf "http://127.0.0.1:6333/readyz" >/dev/null 2>&1; then
    echo "Qdrant ready at http://127.0.0.1:6333"
    exit 0
  fi
  sleep 1
done

echo "Qdrant failed to become ready after 30 checks" >&2
docker logs --tail 30 "${CONTAINER}" >&2 || true
exit 1
