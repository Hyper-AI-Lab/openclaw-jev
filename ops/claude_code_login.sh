#!/usr/bin/env bash
# Sign Aura's coding runs in with Kirill's Claude subscription: a one-year token from
# `claude setup-token`, stored where only systemd reads it, then checked with a real call.
#
#   bash ops/claude_code_login.sh           # browser flow: open the link, approve, paste the code back
#   bash ops/claude_code_login.sh --paste   # paste a token made elsewhere with `claude setup-token`
set -euo pipefail

RMP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${RMP_ROOT}/venv/bin/python"
CODER=aura-coder
CLAUDE=/home/aura-coder/.local/bin/claude

die() { echo "[claude-login] ERROR: $*" >&2; exit 1; }
[[ "$(id -u)" == "0" ]] || die "run as root"
[[ -x "${CLAUDE}" ]] || die "Claude Code is not installed; run: bash ${RMP_ROOT}/ops/setup_aura_coder.sh"

store() {
  # The token arrives on stdin so it never shows up in a process list.
  (cd "${RMP_ROOT}" && "${PY}" -c '
import sys
from app.coding.credentials import write_token
from app.coding.units import TOKEN_ENV_FILE, TOKEN_META_FILE
meta = write_token(sys.stdin.read().strip(), TOKEN_ENV_FILE, TOKEN_META_FILE)
print(f"[claude-login] token stored ({meta[\"length\"]} chars, fingerprint {meta[\"fingerprint\"]}, expires {meta[\"expires_at\"][:10]})")
')
}

if [[ "${1:-}" == "--paste" ]]; then
  read -rsp "Paste the token from 'claude setup-token' (input hidden): " token
  echo
  printf '%s' "${token}" | tr -d '[:space:]' | store || die "the token was not stored"
  unset token
else
  work="$(mktemp -d /tmp/claude-login.XXXXXX)"
  trap 'shred -u "${work}/session.log" 2>/dev/null || true; rm -rf "${work}"' EXIT
  mkdir -p "${work}/home"
  chown -R "${CODER}:${CODER}" "${work}"
  chmod 700 "${work}"
  echo "[claude-login] A sign-in link follows. Open it in your browser, approve access for Claude Code,"
  echo "[claude-login] then paste the code it shows back here. The token itself is captured automatically."
  # A wide terminal keeps the printed token on one line (claude-code#54738).
  (cd "${work}" && runuser -u "${CODER}" -- env HOME="${work}/home" TERM="${TERM:-xterm-256color}" \
    script -qfec "stty cols 400 rows 50 2>/dev/null; exec ${CLAUDE} setup-token" "${work}/session.log") \
    || die "claude setup-token did not finish"
  (cd "${RMP_ROOT}" && "${PY}" -c '
import sys
from app.coding.credentials import extract_token
token = extract_token(open(sys.argv[1], encoding="utf-8", errors="replace").read())
if not token:
    sys.exit("[claude-login] no token found in the setup-token output; rerun with --paste")
sys.stdout.write(token)
' "${work}/session.log") | store || die "the token was not stored"
fi

echo "[claude-login] checking the token with a real Claude Code call…"
exec "${PY}" "${RMP_ROOT}/ops/claude_code_smoke.py"
