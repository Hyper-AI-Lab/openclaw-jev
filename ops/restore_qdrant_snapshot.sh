#!/usr/bin/env bash
# Restore every collection of a Qdrant full snapshot (qdrant-full.snapshot from ops/backup.sh) into a running Qdrant.
#
#   bash ops/restore_qdrant_snapshot.sh <backup-dir>/qdrant-full.snapshot [http://127.0.0.1:6333]
#
# Qdrant restores a *full* snapshot only at startup (`qdrant --storage-snapshot <file>`). Its archive holds one
# snapshot per collection (`<collection>-<peer id>-<date>.snapshot`), and each of those restores through the API,
# into the live server, replacing the collection's data (priority=snapshot).
set -euo pipefail

die() { echo "[restore-qdrant] ERROR: $*" >&2; exit 1; }

SNAPSHOT="${1:-}"
URL="${2:-}"
[[ -s "${SNAPSHOT}" ]] || die "usage: $0 <qdrant-full.snapshot> [qdrant url]; '${SNAPSHOT}' is missing or empty"
if [[ -z "${URL}" ]]; then
  RMP_ROOT="${RMP_ROOT:-/root/.openclaw/rmp}"
  PY="${RMP_PYTHON:-${RMP_ROOT}/venv/bin/python}"
  URL="$(cd "${RMP_ROOT}" && "${PY}" -c '
from app.config import get_vector_memory_config
c = get_vector_memory_config()
print("http://%s:%d" % (c.get("qdrant_host") or "127.0.0.1", int(c.get("qdrant_port") or 6333)))')" \
    || die "could not read the Qdrant address from settings; pass it as the second argument"
fi
curl -fsS -m 10 --retry 12 --retry-delay 5 --retry-all-errors -o /dev/null "${URL}/readyz" || die "Qdrant at ${URL} is not ready"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
tar -xf "${SNAPSHOT}" -C "${work}" || die "${SNAPSHOT} is not a readable archive"
shopt -s nullglob
collections=("${work}"/*.snapshot)
shopt -u nullglob
(( ${#collections[@]} )) || die "${SNAPSHOT} holds no collection snapshots"

failed=()
for file in "${collections[@]}"; do
  name="$(basename "${file}")"
  collection="$(sed -E 's/-[0-9]+-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}\.snapshot$//' <<<"${name}")"
  if [[ "${collection}" == "${name}" || -z "${collection}" ]]; then
    echo "[restore-qdrant] cannot read the collection name from ${name}" >&2
    failed+=("${name}")
    continue
  fi
  echo "[restore-qdrant] restoring ${collection} from ${name}..."
  if curl -fsS -m 3600 -X POST -F "snapshot=@${file}" \
      "${URL}/collections/${collection}/snapshots/upload?priority=snapshot&wait=true" >/dev/null; then
    echo "[restore-qdrant] ${collection} restored"
  else
    failed+=("${collection}")
  fi
done

if (( ${#failed[@]} )); then
  die "not restored: ${failed[*]}"
fi
echo "[restore-qdrant] restored ${#collections[@]} collection(s) into ${URL}"
