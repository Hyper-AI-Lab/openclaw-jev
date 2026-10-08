#!/usr/bin/env bash
# Prepare this host for Aura's coding runs: the aura-coder user and its directories, a pinned
# Claude Code, Claude Code's lockdown policy for coding units (bound over /etc/claude-code inside
# them) and the host's relaxed policy for Aura's direct sessions, and the firewall that keeps
# aura-coder off this host's services. Idempotent; run as root. The subscription token comes
# separately from ops/claude_code_login.sh.
#
#   bash ops/setup_aura_coder.sh
set -euo pipefail

RMP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${RMP_ROOT}/venv/bin/python"
CODER=aura-coder
CODER_HOME=/home/aura-coder
# Claude Code runs as root in Aura's direct sessions, so it lives root-owned outside every path aura-coder can
# write (app/coding/units.py CLAUDE_INSTALL_DIR), installed from Anthropic's release bucket and checked against
# the release manifest's sha256 before it is used.
CLAUDE_DIR=/opt/claude-code
CLAUDE="${CLAUDE_DIR}/bin/claude"
RELEASES=https://downloads.claude.ai/claude-code-releases

log() { echo "[setup-aura-coder] $*"; }
die() { echo "[setup-aura-coder] ERROR: $*" >&2; exit 1; }
as_coder() { (cd "${CODER_HOME}" && runuser -u "${CODER}" -- env HOME="${CODER_HOME}" PATH="${CODER_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@"); }

[[ "$(id -u)" == "0" ]] || die "run as root"
for cmd in curl git nft runuser systemd-run systemctl; do
  command -v "${cmd}" >/dev/null || die "missing command: ${cmd}"
done
VERSION="${CLAUDE_CODE_VERSION:-$(cd "${RMP_ROOT}" && "${PY}" -c 'from app.config import get_coding_config; print(get_coding_config()["claude_version"])')}"
[[ "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "bad Claude Code version: ${VERSION}"

if ! id -u "${CODER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "${CODER_HOME}" --shell /bin/bash --user-group \
    --comment "Aura coding runner (Claude Code)" "${CODER}"
  log "created user ${CODER}"
fi
passwd -l "${CODER}" >/dev/null
chmod 700 "${CODER_HOME}"

install -d -o root -g root -m 755 /srv/aura-code
install -d -o root -g "${CODER}" -m 750 /srv/aura-code/jobs
install -d -o root -g root -m 755 /srv/aura-code/venvs
install -d -o root -g root -m 755 /srv/aura-code/policy
install -d -o "${CODER}" -g "${CODER}" -m 700 /srv/aura-code/cache
# Run streams and exit records: written by systemd as root, never by aura-coder.
install -d -o root -g root -m 700 /srv/aura-code/runs
install -d -o root -g root -m 700 /etc/aura-coder
log "directories ready under /srv/aura-code and /etc/aura-coder"

MANAGED=/etc/claude-code/managed-settings.json
install_claude() {
  local version="$1" pin="$2" platform tmp sum
  case "$(uname -m)" in x86_64) platform=linux-x64 ;; aarch64) platform=linux-arm64 ;; *) die "unsupported machine $(uname -m)" ;; esac
  if ldd /bin/ls 2>&1 | grep -q musl; then platform="${platform}-musl"; fi
  tmp="$(mktemp -d)"
  trap 'rm -rf "${tmp}"' EXIT
  curl -fsSL "${RELEASES}/${version}/manifest.json" -o "${tmp}/manifest.json"
  sum="$("${PY}" -c 'import json, sys; print(json.load(open(sys.argv[1])).get("platforms", {}).get(sys.argv[2], {}).get("checksum", ""))' "${tmp}/manifest.json" "${platform}")"
  [[ "${sum}" =~ ^[a-f0-9]{64}$ ]] || die "no checksum for ${platform} in the ${version} manifest"
  # The manifest sits next to the binary, so it only proves the download is intact; the pin in app/config.py
  # (reviewed with the version bump) decides what root may run.
  [[ "${sum}" == "${pin}" ]] || die "the ${version} manifest's sha256 for ${platform} is not the pinned coding.claude_sha256"
  curl -fsSL "${RELEASES}/${version}/${platform}/claude" -o "${tmp}/claude"
  [[ "$(sha256sum "${tmp}/claude" | cut -d' ' -f1)" == "${pin}" ]] || die "checksum mismatch for Claude Code ${version}"
  install -d -o root -g root -m 755 "${CLAUDE_DIR}" "${CLAUDE_DIR}/versions" "${CLAUDE_DIR}/bin"
  install -o root -g root -m 755 "${tmp}/claude" "${CLAUDE_DIR}/versions/${version}"
  # Swap the link in one rename, so a turn starting meanwhile finds the old or the new binary, never neither.
  ln -sfn "../versions/${version}" "${CLAUDE}.new"
  mv -Tf "${CLAUDE}.new" "${CLAUDE}"
  rm -rf "${tmp}"
  trap - EXIT
  # Keep this version and the newest other one, for a quick rollback; older ones go.
  ls -1t "${CLAUDE_DIR}/versions" | grep -vxF "${version}" | tail -n +2 | while read -r old; do rm -f "${CLAUDE_DIR}/versions/${old}"; done || true
}
# The version is the versions/<version> name the link points to, once that file is there: never run to find out.
installed() { [[ -f "${CLAUDE}" && -x "${CLAUDE}" ]] && basename "$(readlink "${CLAUDE}")" || echo none; }
PIN="${CLAUDE_CODE_SHA256:-$(cd "${RMP_ROOT}" && "${PY}" -c 'from app.config import get_coding_config; print(get_coding_config().get("claude_sha256", ""))')}"
[[ "${PIN}" =~ ^[a-f0-9]{64}$ ]] || die "no sha256 pinned for Claude Code ${VERSION} (coding.claude_sha256)"
current="$(installed)"
if [[ "${current}" != "${VERSION}" ]]; then
  log "installing Claude Code ${VERSION} root-owned at ${CLAUDE_DIR} (was: ${current})"
  install_claude "${VERSION}" "${PIN}"
  current="$(installed)"
fi

install -d -o root -g root -m 755 /etc/claude-code
install -o root -g root -m 644 "${RMP_ROOT}/ops/aura_coder/managed-settings.json" /srv/aura-code/policy/managed-settings.json
install -o root -g root -m 644 "${RMP_ROOT}/ops/claude_host/managed-settings.json" "${MANAGED}"
log "coding policy at /srv/aura-code/policy, host policy at ${MANAGED}"
install -o root -g root -m 755 "${RMP_ROOT}/ops/aura_github.sh" /usr/local/bin/aura-github
log "aura-github installed for Aura's direct sessions"
[[ "${current}" == "${VERSION}" ]] || die "Claude Code is ${current:-missing}, expected ${VERSION}"
log "Claude Code ${current} at ${CLAUDE}"

as_coder git config --global user.name "Aura (Claude Code)"
as_coder git config --global user.email "aura-coder@aura.local"
as_coder git config --global init.defaultBranch main
as_coder git config --global advice.detachedHead false

install -o root -g root -m 644 "${RMP_ROOT}/systemd/aura-coder-firewall.service" /etc/systemd/system/aura-coder-firewall.service
systemctl daemon-reload
systemctl enable aura-coder-firewall.service >/dev/null 2>&1
systemctl restart aura-coder-firewall.service
systemctl is-active --quiet aura-coder-firewall.service || die "aura-coder-firewall.service is not active"
(cd "${RMP_ROOT}" && "${PY}" -m app.coding.firewall status) || die "firewall status check failed"

if [[ -f /etc/aura-coder/claude.env ]]; then
  log "token present; check it with: ${PY} ${RMP_ROOT}/ops/claude_code_smoke.py"
else
  log "no token yet; sign in with: bash ${RMP_ROOT}/ops/claude_code_login.sh"
fi
log "done"
