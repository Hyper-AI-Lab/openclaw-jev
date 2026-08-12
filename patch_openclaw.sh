#!/bin/bash
# RMP Post-Install Patch Script — run after any OpenClaw update.
# Keeps OpenClaw aligned with RMP architecture:
#   - hook persistence (typed hooks always fire when runner exists)
#   - suppress native announce/Slack for RMP-owned sessions
#   - minimal bootstrap for RMP internal sessions (task/verify/intake)
# Model fallbacks (MiniMax → DeepSeek → GLM) are INTENTIONAL — do not disable them.
set -euo pipefail

DIST_DIR="/usr/lib/node_modules/openclaw/dist"
PATCHED=0
RMP_GUARD='rmp_(task|verify|intake)_'

echo "=== RMP OpenClaw Patch Script ==="
echo "Target: $DIST_DIR"
echo ""

if [ ! -d "$DIST_DIR" ]; then
    echo "ERROR: OpenClaw dist directory not found at $DIST_DIR"
    exit 1
fi

# Narrow file set via ripgrep when available (full-tree sed is very slow).
mapfile -t CANDIDATES < <(
  if command -v rg >/dev/null 2>&1; then
    rg -l --glob '*.js' \
      'hasHooks\("before_message_write"\)|filterBootstrapFilesForSession|runSubagentAnnounceFlow|async function deliverReplies|fallbackConfigured = false && hasConfiguredModelFallbacks|function normalizeAgentPayload|allowUnsafeExternalContent: value\.allowUnsafeExternalContent' \
      "$DIST_DIR" 2>/dev/null || true
  else
    find "$DIST_DIR" -name '*.js'
  fi
)

if [ "${#CANDIDATES[@]}" -eq 0 ]; then
  echo "WARN: no candidate files matched; falling back to full dist scan"
  mapfile -t CANDIDATES < <(find "$DIST_DIR" -name '*.js')
fi

echo "Scanning ${#CANDIDATES[@]} candidate file(s)"

restore_model_fallbacks() {
    local f="$1"
    if grep -q 'const fallbackConfigured = false && hasConfiguredModelFallbacks({' "$f" 2>/dev/null; then
        sed -i 's/const fallbackConfigured = false && hasConfiguredModelFallbacks({/const fallbackConfigured = hasConfiguredModelFallbacks({/' "$f"
        echo "  Restored model fallbacks in $(basename "$f")"
        PATCHED=$((PATCHED + 1))
    fi
}

patch_file() {
    local f="$1"
    local applied=""

    # Patch 1: Hook persistence
    if grep -q 'hookRunner?.hasHooks("before_message_write") ? (event)' "$f" 2>/dev/null; then
        sed -i 's/hookRunner?.hasHooks("before_message_write") ? (event)/hookRunner ? (event)/g' "$f"
        applied="${applied} hook-persistence"
    fi
    if grep -q 'if (!hookRunner?.hasHooks("before_message_write")) return params.message;' "$f" 2>/dev/null; then
        sed -i 's/if (!hookRunner?.hasHooks("before_message_write")) return params.message;/if (!hookRunner) return params.message;/g' "$f"
        applied="${applied} hook-persistence-neg"
    fi
    if grep -q 'if (hookRunner?.hasHooks("before_message_write")) {' "$f" 2>/dev/null; then
        sed -i 's/if (hookRunner?.hasHooks("before_message_write")) {/if (hookRunner) {/g' "$f"
        applied="${applied} hook-persistence-pos"
    fi

    # Patch 2: Announce suppression for RMP-owned sessions
    if grep -q 'async function runSubagentAnnounceFlow(params) {' "$f" 2>/dev/null \
       && ! grep -q "$RMP_GUARD" "$f" 2>/dev/null; then
        sed -i 's/async function runSubagentAnnounceFlow(params) {/async function runSubagentAnnounceFlow(params) { if (params.childSessionKey \&\& \/rmp_(task|verify|intake)_\/.test(params.childSessionKey)) return true;/' "$f"
        applied="${applied} announce-suppress"
    fi

    # Patch 3: Minimal bootstrap for RMP internal sessions
    if grep -q 'function filterBootstrapFilesForSession(files, sessionKey)' "$f" 2>/dev/null \
       && ! grep -q "$RMP_GUARD" "$f" 2>/dev/null; then
        sed -i 's/function filterBootstrapFilesForSession(files, sessionKey) {/function filterBootstrapFilesForSession(files, sessionKey) { if (sessionKey \&\& \/rmp_(task|verify|intake)_\/.test(sessionKey)) return files.filter((file) => file.name === "TOOLS.md" || file.name === "tools.md");/' "$f"
        applied="${applied} rmp-minimal-bootstrap"
    fi

    # Patch 4: Native Slack deliverReplies → RMP suppressor
    if grep -qE 'async function deliverReplies(\$[0-9]+)?\(params\) \{' "$f" 2>/dev/null \
       && ! grep -q '__RMP_SUPPRESS_NATIVE_SLACK' "$f" 2>/dev/null; then
        sed -i -E 's/async function deliverReplies(\$[0-9]+)?\(params\) \{/async function deliverReplies\1(params) { try { if (typeof globalThis.__RMP_SUPPRESS_NATIVE_SLACK === "function" \&\& globalThis.__RMP_SUPPRESS_NATIVE_SLACK(params)) return; } catch (_) {} /' "$f"
        applied="${applied} slack-rmp-suppress"
    fi

    # Patch 5a: OpenClaw 2026.7+ dropped allowUnsafeExternalContent from HTTP
    # /hooks/agent normalizeAgentPayload — without it, EXTERNAL wrap forces NO_REPLY
    # on structured JSON intake. Pass through flag + auto-enable for RMP sessions.
    if grep -q 'function normalizeAgentPayload(payload)' "$f" 2>/dev/null \
       && ! grep -q 'RMP_ALLOW_UNSAFE_EXTERNAL' "$f" 2>/dev/null; then
        python3 - "$f" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
needle = "\t\t\ttimeoutSeconds: typeof timeoutRaw === \"number\" && Number.isFinite(timeoutRaw) && timeoutRaw > 0 ? Math.floor(timeoutRaw) : void 0\n\t\t}"
insert = (
    "\t\t\ttimeoutSeconds: typeof timeoutRaw === \"number\" && Number.isFinite(timeoutRaw) && timeoutRaw > 0 ? Math.floor(timeoutRaw) : void 0,\n"
    "\t\t\t/* RMP_ALLOW_UNSAFE_EXTERNAL */\n"
    "\t\t\tallowUnsafeExternalContent: payload.allowUnsafeExternalContent === true || "
    "(typeof sessionKey === \"string\" && /rmp_(task|verify|intake)_/.test(sessionKey)) ? true : void 0\n"
    "\t\t}"
)
if needle not in text:
    sys.exit(0)
path.write_text(text.replace(needle, insert, 1))
print("patched-normalize")
PY
        if grep -q 'RMP_ALLOW_UNSAFE_EXTERNAL' "$f" 2>/dev/null; then
            applied="${applied} allow-unsafe-passthrough"
        fi
    fi

    # Patch 5b: Force allowUnsafe for RMP session keys at dispatch (belt-and-suspenders).
    if grep -q 'allowUnsafeExternalContent: value.allowUnsafeExternalContent,' "$f" 2>/dev/null \
       && ! grep -q 'RMP_FORCE_ALLOW_UNSAFE' "$f" 2>/dev/null; then
        sed -i 's/allowUnsafeExternalContent: value.allowUnsafeExternalContent,/allowUnsafeExternalContent: value.allowUnsafeExternalContent === true || (typeof value.sessionKey === "string" \&\& \/rmp_(task|verify|intake)_\/.test(value.sessionKey)) \/* RMP_FORCE_ALLOW_UNSAFE *\/,/g' "$f"
        applied="${applied} allow-unsafe-rmp-force"
    fi

    # Patch 6: Fast LLM idle silence (5s) then rotate NVIDIA keys — do not sit 120s.
    if grep -q 'const DEFAULT_LLM_IDLE_TIMEOUT_MS = 12e4;' "$f" 2>/dev/null; then
        sed -i 's/const DEFAULT_LLM_IDLE_TIMEOUT_MS = 12e4;/const DEFAULT_LLM_IDLE_TIMEOUT_MS = 5e3; \/* RMP_LLM_IDLE_5S *\//' "$f"
        applied="${applied} llm-idle-5s"
    fi

    if [ -n "$applied" ]; then
        echo "  Patched $(basename "$f"):$applied"
        PATCHED=$((PATCHED + 1))
    fi
}

for f in "${CANDIDATES[@]}"; do
    [ -f "$f" ] || continue
    restore_model_fallbacks "$f"
    patch_file "$f"
done

echo ""
echo "Done. Patched/restored $PATCHED files."
echo "Note: model fallbacks left ENABLED (MiniMax → DeepSeek → GLM)."
