#!/usr/bin/env bash
# The aura-coder firewall by hand (normally aura-coder-firewall.service applies it at boot).
#   bash ops/aura_coder_firewall.sh {apply,remove,status,render}
set -euo pipefail
RMP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${RMP_ROOT}"
exec "${RMP_ROOT}/venv/bin/python" -m app.coding.firewall "${1:-status}"
