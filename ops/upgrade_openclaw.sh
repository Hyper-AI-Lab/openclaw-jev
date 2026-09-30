#!/usr/bin/env bash
# One-shot OpenClaw upgrade for this RMP host.
# pre-flight → backup → node check → npm i -g → plugins update → doctor --fix → config-key guard →
# patch_openclaw.sh → verify → skills → optional restart.
#
# Never: openclaw onboard, openclaw update, doctor --force, hand-edit dist.
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
OPENCLAW_HOME="/root/.openclaw"
DIST_DIR="/usr/lib/node_modules/openclaw/dist"
BACKUP_ROOT="${RMP_ROOT}/data/backups"
MIN_NODE="24.16.0"
PACKAGE="${OPENCLAW_PACKAGE:-openclaw@latest}"
SKIP_NPM="${SKIP_NPM:-0}"
SKIP_DOCTOR="${SKIP_DOCTOR:-0}"
SKIP_RESTART="${SKIP_RESTART:-0}"

usage() {
  cat <<'EOF'
Usage: bash ops/upgrade_openclaw.sh

Env:
  OPENCLAW_PACKAGE   npm spec (default openclaw@latest)
  SKIP_NPM=1         skip npm install (patch/verify/restart only)
  SKIP_DOCTOR=1      skip openclaw doctor --fix
  SKIP_RESTART=1     skip systemd restart
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

log() { echo "[upgrade-openclaw] $*"; }
die() { echo "[upgrade-openclaw] ERROR: $*" >&2; exit 1; }

require_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1"; }

version_ge() {
  # true if $1 >= $2 (dotted numeric, ignores leading v)
  python3 - "$1" "$2" <<'PY'
import sys
def parts(s):
    s = s.lstrip("vV")
    out = []
    for p in s.split("."):
        n = ""
        for ch in p:
            if ch.isdigit():
                n += ch
            else:
                break
        out.append(int(n or 0))
    return out
a, b = parts(sys.argv[1]), parts(sys.argv[2])
n = max(len(a), len(b))
a += [0] * (n - len(a))
b += [0] * (n - len(b))
sys.exit(0 if a >= b else 1)
PY
}

active_user_tasks() {
  cd "${RMP_ROOT}" && ./venv/bin/python -c "
from app.production.canary_sentinel import count_active_user_tasks_sync
print(count_active_user_tasks_sync())
"
}

restore_tools_md() {
  local tools="${OPENCLAW_HOME}/workspace/TOOLS.md"
  local agents="${OPENCLAW_HOME}/workspace/AGENTS.md"
  if [[ -f "${tools}" ]]; then
    log "TOOLS.md present"
    return 0
  fi
  log "TOOLS.md missing after doctor — restoring"
  if [[ -f "${BACKUP_DIR}/workspace/TOOLS.md" ]]; then
    cp -a "${BACKUP_DIR}/workspace/TOOLS.md" "${tools}"
    log "Restored TOOLS.md from backup"
    return 0
  fi
  python3 - "${agents}" "${tools}" <<'PY'
import pathlib, sys
agents, tools = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
if not agents.is_file():
    raise SystemExit("AGENTS.md missing; cannot restore TOOLS.md")
text = agents.read_text()
idx = text.find("# TOOLS.md")
if idx < 0:
    raise SystemExit("AGENTS.md has no TOOLS.md section")
tools.write_text(text[idx:])
print("extracted TOOLS.md from AGENTS.md")
PY
}

guard_config_keys() {
  local current="${OPENCLAW_HOME}/openclaw.json"
  local backup="${BACKUP_DIR}/openclaw.json"
  python3 - "${current}" "${backup}" <<'PY'
import json, sys
from pathlib import Path

current_path = Path(sys.argv[1])
backup_path = Path(sys.argv[2])
cfg = json.loads(current_path.read_text())
bak = json.loads(backup_path.read_text()) if backup_path.is_file() else {}

changed = []

hooks = cfg.setdefault("hooks", {})
if hooks.get("allowRequestSessionKey") is not True:
    hooks["allowRequestSessionKey"] = True
    changed.append("hooks.allowRequestSessionKey")
internal = hooks.setdefault("internal", {})
if internal.get("enabled") is not True:
    bak_internal = (bak.get("hooks") or {}).get("internal") or {}
    if bak_internal:
        hooks["internal"] = bak_internal
        hooks["internal"]["enabled"] = True
    else:
        internal["enabled"] = True
    changed.append("hooks.internal.enabled")

want_prefixes = ["hook:", "agent:main:rmp_"]
prefixes = list(hooks.get("allowedSessionKeyPrefixes") or [])
for p in want_prefixes:
    if p not in prefixes:
        prefixes.append(p)
        changed.append(f"hooks.allowedSessionKeyPrefixes:{p}")
if prefixes:
    hooks["allowedSessionKeyPrefixes"] = prefixes

channels = cfg.setdefault("channels", {})
slack = channels.setdefault("slack", {})
want_streaming = {"mode": "off", "nativeTransport": False}
if slack.get("streaming") != want_streaming:
    slack["streaming"] = want_streaming
    changed.append("channels.slack.streaming")

plugins = cfg.setdefault("plugins", {})
load = plugins.setdefault("load", {})
paths = list(load.get("paths") or [])
required_paths = [
    "/root/.openclaw/plugins/rmp_adapter",
    "/root/.openclaw/plugins/aura_web",
]
bak_paths = ((bak.get("plugins") or {}).get("load") or {}).get("paths") or []
for p in [*required_paths, *bak_paths]:
    if p and p not in paths:
        paths.append(p)
        changed.append(f"plugins.load.paths:{p}")
load["paths"] = paths

# Model primary/fallbacks come from RMP model_policy (not backup MiniMax/GLM).
models = cfg.setdefault("models", {}).setdefault("providers", {})
bak_nvidia = ((bak.get("models") or {}).get("providers") or {}).get("nvidia") or {}
nvidia = models.get("nvidia") or {}
if bak_nvidia.get("models") and (not nvidia.get("models") or len(nvidia.get("models") or []) < 2):
    models["nvidia"] = bak_nvidia
    changed.append("models.providers.nvidia")

if changed:
    current_path.write_text(json.dumps(cfg, indent=2) + "\n")
    print("restored: " + ", ".join(changed))
else:
    print("RMP-critical config keys intact")
PY
  log "Apply RMP model policy (openai/gpt-6-luna + NVIDIA fallbacks, no GLM)"
  "${RMP_ROOT}/venv/bin/python" - <<'PY'
import json, sys
sys.path.insert(0, "/root/.openclaw/rmp")
from app.llm.model_policy import apply_openclaw_policy
result = apply_openclaw_policy()
print(json.dumps({"changed": result["changed"], "primary": result["primary"], "fallbacks": result["fallbacks"]}))
PY
}

require_cmd node
require_cmd npm
require_cmd openclaw
require_cmd python3
require_cmd systemctl

NODE_VER="$(node --version)"
version_ge "${NODE_VER}" "${MIN_NODE}" || die "Node ${NODE_VER} < ${MIN_NODE} (required)"

BEFORE_OC="$(openclaw --version 2>/dev/null | head -1 || true)"
log "node=${NODE_VER} npm=$(npm --version) openclaw=${BEFORE_OC}"

UNIT_START="$(systemctl show -p ExecStart --value openclaw-gateway.service 2>/dev/null || true)"
if [[ "${UNIT_START}" != *"/usr/bin/openclaw"* ]]; then
  log "WARN: openclaw-gateway ExecStart is not /usr/bin/openclaw: ${UNIT_START}"
fi

if [[ "${SKIP_NPM}" != "1" ]]; then
  log "Pre-flight: rehearse ${PACKAGE} in a scratch directory (Node range, RMP patches, transcript format)"
  "${RMP_ROOT}/venv/bin/python" "${RMP_ROOT}/ops/openclaw_preflight.py" "${PACKAGE}" \
    || die "pre-flight refused ${PACKAGE}; nothing was backed up, stopped or installed"
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="${BACKUP_ROOT}/openclaw-update-${STAMP}"
mkdir -p "${BACKUP_DIR}/plugins" "${BACKUP_DIR}/workspace" "${BACKUP_DIR}/agents/main/agent"
log "Backup → ${BACKUP_DIR}"

cp -a "${OPENCLAW_HOME}/openclaw.json" "${BACKUP_DIR}/openclaw.json"
if [[ -f "${OPENCLAW_HOME}/agents/main/agent/auth-profiles.json" ]]; then
  cp -a "${OPENCLAW_HOME}/agents/main/agent/auth-profiles.json" \
    "${BACKUP_DIR}/agents/main/agent/auth-profiles.json"
fi
for plug in rmp_adapter aura_web; do
  if [[ -d "${OPENCLAW_HOME}/plugins/${plug}" ]]; then
    cp -a "${OPENCLAW_HOME}/plugins/${plug}" "${BACKUP_DIR}/plugins/${plug}"
  fi
done
# Sessions, transcripts and state, while the gateway still runs: the new version migrates them.
"${RMP_ROOT}/venv/bin/python" "${RMP_ROOT}/ops/backup_openclaw_state.py" backup --dest "${BACKUP_DIR}/openclaw-state" \
  || die "OpenClaw store backup failed; nothing was stopped or installed"
if [[ -f "${OPENCLAW_HOME}/workspace/TOOLS.md" ]]; then
  cp -a "${OPENCLAW_HOME}/workspace/TOOLS.md" "${BACKUP_DIR}/workspace/TOOLS.md"
fi
if [[ -f "${OPENCLAW_HOME}/workspace/AGENTS.md" ]]; then
  cp -a "${OPENCLAW_HOME}/workspace/AGENTS.md" "${BACKUP_DIR}/workspace/AGENTS.md"
fi
{
  echo "node=${NODE_VER}"
  echo "npm=$(npm --version)"
  echo "openclaw_before=${BEFORE_OC}"
  for pkg in "${OPENCLAW_HOME}"/npm/projects/*/node_modules/@openclaw/*/package.json; do
    [[ -f "${pkg}" ]] || continue
    node -p 'const p = require(process.argv[1]); `plugin_before=${p.name}@${p.version}`' "${pkg}"
  done
  echo "stamp=${STAMP}"
} > "${BACKUP_DIR}/VERSIONS.txt"
echo "${BACKUP_DIR}" > /tmp/openclaw-upgrade-backup.path
log "Backup complete"

ACTIVE="$(active_user_tasks || true)"
if [[ ! "${ACTIVE}" =~ ^[0-9]+$ ]]; then
  die "could not count active user tasks (got: ${ACTIVE:-empty})"
fi
if [[ "${ACTIVE}" -gt 0 ]]; then
  die "refusing to upgrade: ${ACTIVE} active user task(s). Retry when idle."
fi

if systemctl is-active --quiet openclaw-gateway; then
  log "Stopping openclaw-gateway for upgrade"
  systemctl stop openclaw-gateway
fi

if [[ "${SKIP_NPM}" != "1" ]]; then
  log "npm install -g ${PACKAGE}"
  if ! npm install -g "${PACKAGE}"; then
    log "retrying with --allow-scripts=openclaw"
    npm install -g "${PACKAGE}" --allow-scripts=openclaw
  fi
  AFTER_OC="$(openclaw --version 2>/dev/null | head -1 || true)"
  log "openclaw now: ${AFTER_OC}"
  echo "openclaw_after=${AFTER_OC}" >> "${BACKUP_DIR}/VERSIONS.txt"
  if [[ "${AFTER_OC}" != *"2026."* ]]; then
    die "unexpected openclaw version after install: ${AFTER_OC}"
  fi
else
  log "SKIP_NPM=1 — leaving global package as $(openclaw --version 2>/dev/null | head -1)"
fi

# npm-installed plugins (Slack, Brave, ...) do not follow the core package; their
# hook and debounce contracts must match it.
log "openclaw plugins update --all (newest versions compatible with this core)"
openclaw plugins update --all

if [[ "${SKIP_DOCTOR}" != "1" ]]; then
  log "openclaw doctor --fix --non-interactive (no --force)"
  # Systemd unit is /etc/systemd/system/openclaw-gateway.service, not user systemd.
  # Without this, doctor treats a user-unit .bak as a managed service and refuses.
  OPENCLAW_SERVICE_REPAIR_POLICY=external \
    openclaw doctor --fix --non-interactive
else
  log "SKIP_DOCTOR=1"
fi

guard_config_keys
restore_tools_md

log "Settle OpenClaw session_nodes.entry_valid (keep schema triggers)"
"${RMP_ROOT}/venv/bin/python" "${RMP_ROOT}/ops/settle_openclaw_sessions.py"

log "Sync LLM keys into OpenClaw auth store (SQLite on 2026.9+)"
"${RMP_ROOT}/venv/bin/python" "${RMP_ROOT}/ops/sync_nvidia_keys.py"

log "Applying RMP dist patches"
bash "${RMP_ROOT}/patch_openclaw.sh"
# npm replaced the package directory, and with it the skill links the verifier checks.
bash "${RMP_ROOT}/ops/ensure_openclaw_skills.sh"
bash "${RMP_ROOT}/ops/verify_openclaw_patch.sh"

if [[ "${SKIP_RESTART}" == "1" ]]; then
  log "SKIP_RESTART=1 — gateway left stopped if it was stopped"
  log "Done. Backup: ${BACKUP_DIR}"
  exit 0
fi

ACTIVE="$(active_user_tasks || true)"
if [[ "${ACTIVE}" =~ ^[1-9][0-9]*$ ]]; then
  die "user tasks appeared during upgrade (${ACTIVE}); not restarting. Backup: ${BACKUP_DIR}"
fi

log "Restarting RMP + gateway"
bash "${RMP_ROOT}/ops/restart_rmp.sh"
log "Done. Backup: ${BACKUP_DIR}"
log "Rollback: reinstall the version in ${BACKUP_DIR}/VERSIONS.txt (openclaw_before), openclaw plugins install each plugin_before spec, restore openclaw.json + plugins, stop the gateway and run ops/backup_openclaw_state.py restore ${BACKUP_DIR}/openclaw-state --yes, bash patch_openclaw.sh, restart"
