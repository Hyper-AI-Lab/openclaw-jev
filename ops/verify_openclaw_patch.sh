#!/usr/bin/env bash
# Verify RMP OpenClaw patches are applied after npm update.
set -euo pipefail

DIST_DIR="/usr/lib/node_modules/openclaw/dist"
FAIL=0

echo "=== RMP OpenClaw Patch Verification ==="

if [[ ! -d "$DIST_DIR" ]]; then
  echo "FAIL: OpenClaw dist not found at $DIST_DIR"
  exit 1
fi

check_absent() {
  local pattern="$1"
  local label="$2"
  if grep -rqE "$pattern" "$DIST_DIR" --include='*.js' 2>/dev/null; then
    echo "FAIL: $label still present (patch not applied)"
    FAIL=1
  else
    echo "OK: $label absent"
  fi
}

check_present() {
  local pattern="$1"
  local label="$2"
  local count
  count=$(grep -rE "$pattern" "$DIST_DIR" --include='*.js' 2>/dev/null | wc -l || true)
  if [[ "$count" -gt 0 ]]; then
    echo "OK: $label present ($count matches)"
  else
    echo "FAIL: $label not found in dist"
    FAIL=1
  fi
}

check_absent 'hookRunner\?\.hasHooks\("before_message_write"\)' 'hasHooks before_message_write guards'
# Architecture uses intentional model fallbacks — legacy disable must stay gone.
check_absent 'fallbackConfigured = false && hasConfiguredModelFallbacks' 'legacy no-fallback disable'
check_absent 'const DEFAULT_LLM_IDLE_TIMEOUT_MS = 12e4' '120s LLM idle timeout'
check_present 'RMP_HOOK_PERSISTENCE' 'hook-persistence runner-only guards'
check_present 'RMP_ANNOUNCE_SUPPRESS' 'announce-suppress for rmp_* sessions'
check_present 'RMP_MINIMAL_BOOTSTRAP' 'rmp-minimal-bootstrap TOOLS.md only'
check_present '__RMP_SUPPRESS_NATIVE_SLACK' 'slack-rmp-suppress patch'
check_present 'RMP_ALLOW_UNSAFE_EXTERNAL|RMP_FORCE_ALLOW_UNSAFE' 'allowUnsafeExternalContent RMP passthrough'
check_present 'RMP_LLM_IDLE_5S' 'llm-idle-5s'
check_present 'RMP_OPENAI_FIRST_BYTE_20S' 'openai-first-byte-20s'
check_present 'RMP_410_SKIP' '410-skip-model-not-found'
check_present 'RMP_SESSION_PLACEHOLDER_SKIP' 'session-canonical-placeholder-skip'
check_present 'RMP_SESSION_TS_DRIFT' 'session-updatedAt-drift'
check_present 'RMP_SKIP_LOCAL_PLACEMENT_CLEANUP' 'skip-local-placement-cleanup'

if [[ "$FAIL" -ne 0 ]]; then
  echo ""
  echo "Run: bash /root/.openclaw/rmp/patch_openclaw.sh"
  exit 1
fi

echo ""
echo "Patch verification passed."

MOLTMARKET_SKILL="/usr/lib/node_modules/openclaw/skills/moltmarket/SKILL.md"
if [[ -f "$MOLTMARKET_SKILL" ]]; then
  echo "OK: MoltMarket SKILL.md at ${MOLTMARKET_SKILL}"
else
  echo "FAIL: MoltMarket SKILL.md missing — run ops/ensure_openclaw_skills.sh"
  exit 1
fi

# Confirm configured agent fallbacks still present in openclaw.json
PRIMARY=$(python3 -c "import json; c=json.load(open('/root/.openclaw/openclaw.json')); print(c['agents']['defaults']['model']['primary'])")
FALLBACKS=$(python3 -c "import json; c=json.load(open('/root/.openclaw/openclaw.json')); print(','.join(c['agents']['defaults']['model'].get('fallbacks') or []))")
SUB=$(python3 -c "import json; c=json.load(open('/root/.openclaw/openclaw.json')); s=c['agents']['defaults'].get('subagents',{}).get('model'); print(s if isinstance(s,str) else (s or {}).get('primary',''))")
echo "OK: agent primary=${PRIMARY}"
echo "OK: agent fallbacks=${FALLBACKS}"
echo "OK: subagents model=${SUB}"
if [[ "${PRIMARY}" != "openai/gpt-5-nano" ]]; then
  echo "FAIL: agent primary must be openai/gpt-5-nano (got ${PRIMARY})"
  FAIL=1
fi
if [[ "${FALLBACKS}" == *"glm"* ]]; then
  echo "FAIL: GLM must not be in agent fallbacks (${FALLBACKS})"
  FAIL=1
fi
if [[ "$FAIL" -ne 0 ]]; then
  echo "Run: bash /root/.openclaw/rmp/ops/upgrade_openclaw.sh  (or apply_openclaw_policy)"
  exit 1
fi
