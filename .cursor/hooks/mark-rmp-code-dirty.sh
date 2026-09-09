#!/usr/bin/env bash
set -euo pipefail
input=$(cat || true)
DIRTY_FLAG="${RMP_CODE_DIRTY_FLAG:-/root/.openclaw/rmp/data/.code_dirty}"
mkdir -p "$(dirname "$DIRTY_FLAG")"
if printf '%s' "$input" | grep -Eq '\.openclaw/rmp/(app/|worker\.py|plugins/)|openclaw/rmp/(app/|worker\.py|plugins/)'; then
  touch "$DIRTY_FLAG"
fi
echo '{}'
exit 0
