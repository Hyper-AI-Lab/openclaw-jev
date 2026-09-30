#!/usr/bin/env bash
# Verify RMP OpenClaw patches are applied after npm update, and the model policy holds.
# The patch list is ops/openclaw_patch_audit.py, which patch_openclaw.sh uses too.
#   OPENCLAW_DIST_DIR: another dist to verify (default: the installed one).
set -euo pipefail

RMP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${OPENCLAW_DIST_DIR:-/usr/lib/node_modules/openclaw/dist}"
FAIL=0

echo "=== RMP OpenClaw Patch Verification ==="

if [[ ! -d "$DIST_DIR" ]]; then
  echo "FAIL: OpenClaw dist not found at $DIST_DIR"
  exit 1
fi

if ! python3 "${RMP_ROOT}/ops/openclaw_patch_audit.py" "$DIST_DIR"; then
  echo ""
  echo "Run: bash ${RMP_ROOT}/patch_openclaw.sh"
  exit 1
fi

echo ""
echo "Patch verification passed."

MOLTMARKET_SKILL="$(dirname "$DIST_DIR")/skills/moltmarket/SKILL.md"
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
if [[ "${PRIMARY}" != "openai/gpt-6-luna" ]]; then
  echo "FAIL: agent primary must be openai/gpt-6-luna (got ${PRIMARY})"
  FAIL=1
fi
if [[ "${FALLBACKS}" == *"glm"* ]]; then
  echo "FAIL: GLM must not be in agent fallbacks (${FALLBACKS})"
  FAIL=1
fi
UTILITY=$(python3 -c "import json; c=json.load(open('/root/.openclaw/openclaw.json')); print(repr(c['agents']['defaults'].get('utilityModel')))")
if [[ "${UTILITY}" != "''" ]]; then
  echo "FAIL: agents.defaults.utilityModel must be \"\" so OpenClaw makes no utility-model calls (got ${UTILITY})"
  FAIL=1
else
  echo "OK: utility-model route off"
fi
if [[ "$FAIL" -ne 0 ]]; then
  echo "Run: bash /root/.openclaw/rmp/ops/upgrade_openclaw.sh  (or apply_openclaw_policy)"
  exit 1
fi
