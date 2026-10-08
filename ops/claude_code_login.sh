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
CLAUDE=/opt/claude-code/bin/claude

die() { echo "[claude-login] ERROR: $*" >&2; exit 1; }
[[ "$(id -u)" == "0" ]] || die "run as root"
[[ -x "${CLAUDE}" ]] || die "Claude Code is not installed; run: bash ${RMP_ROOT}/ops/setup_aura_coder.sh"

# The token travels on stdin, so it never shows up in a process list.
store() { (cd "${RMP_ROOT}" && "${PY}" -m app.coding.credentials store); }

if [[ "${1:-}" == "--paste" ]]; then
  read -rsp "Paste the token from 'claude setup-token' (input hidden): " token
  echo
  token="$(printf '%s' "${token}" | tr -d '[:space:]')"
  if (( ${#token} < 100 )); then
    # A token copied from a wrapped terminal arrives in two lines.
    read -rsp "That looks cut (${#token} characters). Paste the rest, or press Enter: " rest
    echo
    token="${token}$(printf '%s' "${rest}" | tr -d '[:space:]')"
    unset rest
  fi
  printf '%s' "${token}" | store || die "the token was not stored"
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
  token="$(cd "${RMP_ROOT}" && "${PY}" -m app.coding.credentials extract "${work}/session.log")" \
    || die "no token found; run: bash ${RMP_ROOT}/ops/claude_code_login.sh --paste"
  printf '%s' "${token}" | store || die "the token was not stored"
  unset token
fi

echo "[claude-login] checking the token with a real Claude Code call…"
exec "${PY}" "${RMP_ROOT}/ops/claude_code_smoke.py"
