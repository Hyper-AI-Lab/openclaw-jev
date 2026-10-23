#!/usr/bin/env bash
# curl for Qdrant's API: every argument goes to curl, and when Qdrant has an API key, curl sends it in the api-key
# header. The key is qdrant_api_key()'s (app/memory/vector.py): QDRANT_API_KEY from the environment when it is set, else
# the one in /etc/rmp/rmp.env. It reaches curl as a config line on stdin, never on a command line, which any local user
# can read. Without a key this is plain curl.
#
#   bash ops/qdrant_curl.sh -fsS http://127.0.0.1:6333/collections
#
# The scripts that call Qdrant (ops/backup.sh, ops/start_qdrant.sh, ops/restore_qdrant_snapshot.sh) go through it, and
# so does every call by hand (docs/runbooks/backup-restore.md). A call through it cannot send a body from stdin.
set -euo pipefail

RMP_ROOT="${RMP_ROOT:-/root/.openclaw/rmp}"
PY="${RMP_PYTHON:-${RMP_ROOT}/venv/bin/python}"

key="$(cd "${RMP_ROOT}" && "${PY}" -c 'from app.memory.vector import qdrant_api_key; print(qdrant_api_key())')" \
  || { echo "qdrant_curl: could not read Qdrant's API key" >&2; exit 1; }
if [[ -n "${key}" ]]; then
  exec curl -K - "$@" <<<"header = \"api-key: ${key}\""
fi
exec curl "$@"
