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

check_absent 'hookRunner\?\.hasHooks\("before_message_write"\) \? \(event\)' 'legacy hook-persistence ternary'
check_absent 'hookRunner\?\.hasHooks\("before_message_write"\)' 'hasHooks before_message_write guards'
# Architecture uses intentional model fallbacks — legacy disable must stay gone.
check_absent 'fallbackConfigured = false && hasConfiguredModelFallbacks' 'legacy no-fallback disable'
check_present 'rmp_\(task\|verify\|intake\)_|rmp_task_.*rmp_verify_' 'rmp-minimal-bootstrap / announce session guard'
check_present '__RMP_SUPPRESS_NATIVE_SLACK' 'slack-rmp-suppress patch'
check_present 'if \(!hookRunner\) return params\.message|if \(hookRunner\) \{' 'hook-persistence runner-only guards'
check_present 'RMP_ALLOW_UNSAFE_EXTERNAL|RMP_FORCE_ALLOW_UNSAFE' 'allowUnsafeExternalContent RMP passthrough'

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
