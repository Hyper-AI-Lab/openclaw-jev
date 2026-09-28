#!/bin/bash
# RMP Post-Install Patch Script — run after any OpenClaw update.
# Keeps OpenClaw aligned with RMP architecture:
#   - hook persistence (typed hooks always fire when runner exists)
#   - suppress native announce/Slack for RMP-owned sessions
#   - minimal bootstrap for RMP internal sessions (task/verify/intake)
#   - allowUnsafe passthrough for RMP JSON intake
#   - LLM idle 5s (fail fast, rotate NVIDIA keys); OpenAI first byte 20s
#   - HTTP 410 skip (model_not_found, not idle-timeout retry)
# Model fallbacks (gpt-5-nano → gpt-oss-20b on NVIDIA) are INTENTIONAL — do not disable them.
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
      'hasHooks\("before_message_write"\)|filterBootstrapFilesForSession|runSubagentAnnounceFlow|async function deliverReplies|fallbackConfigured = false && hasConfiguredModelFallbacks|function normalizeAgentPayload|allowUnsafeExternalContent: value\.allowUnsafeExternalContent|DEFAULT_LLM_IDLE_TIMEOUT_MS|scanCanonicalSqliteSessionEntries|function parseSqliteSessionEntryRecord|status === 410' \
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

    # Patch 1: Hook persistence — typed hasHooks() can miss plugin api.on() hooks.
    if grep -q 'hookRunner?.hasHooks("before_message_write")' "$f" 2>/dev/null; then
        sed -i 's/hookRunner?.hasHooks("before_message_write")/hookRunner \/* RMP_HOOK_PERSISTENCE *\//g' "$f"
        applied="${applied} hook-persistence"
    fi

    # Patch 2: Announce suppression for RMP-owned sessions (2026.9 returns "delivered").
    if grep -q 'async function runSubagentAnnounceFlow(params) {' "$f" 2>/dev/null \
       && ! grep -q 'RMP_ANNOUNCE_SUPPRESS' "$f" 2>/dev/null; then
        python3 - "$f" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
needle = "async function runSubagentAnnounceFlow(params) {"
insert = (
    needle
    + ' if (params.childSessionKey && /rmp_(task|verify|intake)_/.test(params.childSessionKey)) return "delivered"; /* RMP_ANNOUNCE_SUPPRESS */'
)
if needle not in text:
    sys.exit(0)
path.write_text(text.replace(needle, insert, 1))
print("patched-announce")
PY
        if grep -q 'RMP_ANNOUNCE_SUPPRESS' "$f" 2>/dev/null; then
            applied="${applied} announce-suppress"
        fi
    fi

    # Patch 3: Minimal bootstrap for RMP internal sessions.
    # 2026.9 second arg is a session object (or a string key on older dist).
    if grep -q 'function filterBootstrapFilesForSession(files,' "$f" 2>/dev/null \
       && ! grep -q 'RMP_MINIMAL_BOOTSTRAP' "$f" 2>/dev/null; then
        python3 - "$f" <<'PY'
import pathlib, re, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
m = re.search(r"function filterBootstrapFilesForSession\(files, \w+\) \{", text)
if not m:
    sys.exit(0)
insert = (
    m.group(0)
    + ' const __rmpSk = typeof session === "string" ? session : session && session.sessionKey;'
    + ' if (__rmpSk && /rmp_(task|verify|intake)_/.test(__rmpSk)) return files.filter((file) => file.name === "TOOLS.md" || file.name === "tools.md"); /* RMP_MINIMAL_BOOTSTRAP */'
)
# If the param is not named `session`, bind from the actual parameter.
param = re.search(r"function filterBootstrapFilesForSession\(files, (\w+)\)", m.group(0))
arg = param.group(1) if param else "session"
if arg != "session":
    insert = (
        m.group(0)
        + f' const __rmpSk = typeof {arg} === "string" ? {arg} : {arg} && {arg}.sessionKey;'
        + ' if (__rmpSk && /rmp_(task|verify|intake)_/.test(__rmpSk)) return files.filter((file) => file.name === "TOOLS.md" || file.name === "tools.md"); /* RMP_MINIMAL_BOOTSTRAP */'
    )
path.write_text(text[:m.start()] + insert + text[m.end():])
print("patched-bootstrap")
PY
        if grep -q 'RMP_MINIMAL_BOOTSTRAP' "$f" 2>/dev/null; then
            applied="${applied} rmp-minimal-bootstrap"
        fi
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
needles = [
    # 2026.7 — timeoutSeconds inline in the returned object
    (
        "\t\t\ttimeoutSeconds: typeof timeoutRaw === \"number\" && Number.isFinite(timeoutRaw) && timeoutRaw > 0 ? Math.floor(timeoutRaw) : void 0\n\t\t}",
        (
            "\t\t\ttimeoutSeconds: typeof timeoutRaw === \"number\" && Number.isFinite(timeoutRaw) && timeoutRaw > 0 ? Math.floor(timeoutRaw) : void 0,\n"
            "\t\t\t/* RMP_ALLOW_UNSAFE_EXTERNAL */\n"
            "\t\t\tallowUnsafeExternalContent: payload.allowUnsafeExternalContent === true || "
            "(typeof sessionKey === \"string\" && /rmp_(task|verify|intake)_/.test(sessionKey)) ? true : void 0\n"
            "\t\t}"
        ),
    ),
    # 2026.9 — timeoutSeconds already a local, returned as a shorthand property
    (
        "\t\t\tmodel,\n\t\t\tthinking,\n\t\t\ttimeoutSeconds\n\t\t}",
        (
            "\t\t\tmodel,\n"
            "\t\t\tthinking,\n"
            "\t\t\ttimeoutSeconds,\n"
            "\t\t\t/* RMP_ALLOW_UNSAFE_EXTERNAL */\n"
            "\t\t\tallowUnsafeExternalContent: payload.allowUnsafeExternalContent === true || "
            "(typeof sessionKey === \"string\" && /rmp_(task|verify|intake)_/.test(sessionKey)) ? true : void 0\n"
            "\t\t}"
        ),
    ),
]
for needle, insert in needles:
    if needle in text:
        path.write_text(text.replace(needle, insert, 1))
        print("patched-normalize")
        break
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

    # Patch 6b: HTTP 410 is a dead/retired model — skip to next fallback, do not
    # classify as timeout (which becomes a 5s idle retry on the same model).
    if grep -q 'if (status === 410)' "$f" 2>/dev/null \
       && ! grep -q 'RMP_410_SKIP' "$f" 2>/dev/null; then
        python3 - "$f" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
if "RMP_410_SKIP" in text:
    raise SystemExit(0)
old = (
    '\tif (status === 410) {\n'
    '\t\tif (messageReason === "session_expired" || messageReason === "billing" || messageReason === "auth_permanent" || messageReason === "auth") return messageClassification;\n'
    '\t\treturn toReasonClassification("timeout");\n'
    '\t}'
)
new = (
    '\tif (status === 410) {\n'
    '\t\tif (messageReason === "session_expired" || messageReason === "billing" || messageReason === "auth_permanent" || messageReason === "auth") return messageClassification;\n'
    '\t\treturn toReasonClassification("model_not_found"); /* RMP_410_SKIP */\n'
    '\t}'
)
if old in text:
    path.write_text(text.replace(old, new, 1))
    print("patched-410")
PY
        if grep -q 'RMP_410_SKIP' "$f" 2>/dev/null; then
            applied="${applied} 410-skip"
        fi
    fi

    # Patch 6c: OpenAI gets 20s for the first byte (stream creation, first chunk, and
    # the provider's first-event guard). gpt-5-nano's first byte takes about 4s at the
    # median and up to 5s, so a 5s cut re-billed the prompt on roughly half the calls.
    # Gaps between chunks, and every other provider, keep the 5s rule.
    if grep -q 'function streamWithIdleTimeout(baseFn, timeoutMs, onIdleTimeout, opts) {' "$f" 2>/dev/null \
       && ! grep -q 'RMP_OPENAI_FIRST_BYTE_20S' "$f" 2>/dev/null; then
        python3 - "$f" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
if "RMP_OPENAI_FIRST_BYTE_20S" in text:
    raise SystemExit(0)
edits = [
    (
        "const CLOUD_LLM_FIRST_EVENT_TIMEOUT_MS = DEFAULT_LLM_IDLE_TIMEOUT_MS;",
        "const CLOUD_LLM_FIRST_EVENT_TIMEOUT_MS = DEFAULT_LLM_IDLE_TIMEOUT_MS;\n"
        "const RMP_OPENAI_FIRST_BYTE_MS = 2e4; /* RMP_OPENAI_FIRST_BYTE_20S */",
    ),
    (
        "isSelfHostedRuntimeModel ? LOCAL_LLM_FIRST_EVENT_TIMEOUT_MS : CLOUD_LLM_FIRST_EVENT_TIMEOUT_MS, ...timeoutBounds));",
        "isSelfHostedRuntimeModel ? LOCAL_LLM_FIRST_EVENT_TIMEOUT_MS : params?.model?.provider === \"openai\" ? RMP_OPENAI_FIRST_BYTE_MS : CLOUD_LLM_FIRST_EVENT_TIMEOUT_MS, ...timeoutBounds));",
    ),
    (
        "\treturn (model, context, options) => {\n"
        "\t\tconst createIdleTimeoutError = () => /* @__PURE__ */ new Error(`LLM idle timeout (${Math.floor(timeoutMs / 1e3)}s): no response from model`);",
        "\treturn (model, context, options) => {\n"
        "\t\tconst firstByteMs = model?.provider === \"openai\" ? Math.max(timeoutMs, RMP_OPENAI_FIRST_BYTE_MS) : timeoutMs;\n"
        "\t\tconst createIdleTimeoutError = (windowMs = timeoutMs) => /* @__PURE__ */ new Error(`LLM idle timeout (${Math.floor(windowMs / 1e3)}s): no response from model`);",
    ),
    (
        "\t\t\t\tconst timer = setTimeout(() => {\n"
        "\t\t\t\t\tconst error = createIdleTimeoutError();\n"
        "\t\t\t\t\tabortStream(error);\n"
        "\t\t\t\t\tonIdleTimeout?.(error);\n"
        "\t\t\t\t\treject(error);\n"
        "\t\t\t\t}, timeoutMs);",
        "\t\t\t\tconst timer = setTimeout(() => {\n"
        "\t\t\t\t\tconst error = createIdleTimeoutError(firstByteMs);\n"
        "\t\t\t\t\tabortStream(error);\n"
        "\t\t\t\t\tonIdleTimeout?.(error);\n"
        "\t\t\t\t\treject(error);\n"
        "\t\t\t\t}, firstByteMs);",
    ),
    (
        "\t\t\t\t\tconst recentActivity = activeToolMs > 0 && Date.now() - activeToolMs < timeoutMs;\n"
        "\t\t\t\t\tconst isFirstStreamArm = firstArmPending && !streamFirstArmDone;\n"
        "\t\t\t\t\tconst effectiveTimeout = isFirstStreamArm && recentActivity ? Math.max(1, timeoutMs - Math.max(0, Date.now() - activeToolMs)) : timeoutMs;\n"
        "\t\t\t\t\tfirstArmPending = false;\n"
        "\t\t\t\t\tif (isFirstStreamArm) streamFirstArmDone = true;\n"
        "\t\t\t\t\tidleTimer = setTimeout(() => {\n"
        "\t\t\t\t\t\tidleTimer = null;\n"
        "\t\t\t\t\t\tconst error = createIdleTimeoutError();",
        "\t\t\t\t\tconst isFirstStreamArm = firstArmPending && !streamFirstArmDone;\n"
        "\t\t\t\t\tconst armMs = isFirstStreamArm ? firstByteMs : timeoutMs;\n"
        "\t\t\t\t\tconst recentActivity = activeToolMs > 0 && Date.now() - activeToolMs < armMs;\n"
        "\t\t\t\t\tconst effectiveTimeout = isFirstStreamArm && recentActivity ? Math.max(1, armMs - Math.max(0, Date.now() - activeToolMs)) : armMs;\n"
        "\t\t\t\t\tfirstArmPending = false;\n"
        "\t\t\t\t\tif (isFirstStreamArm) streamFirstArmDone = true;\n"
        "\t\t\t\t\tidleTimer = setTimeout(() => {\n"
        "\t\t\t\t\t\tidleTimer = null;\n"
        "\t\t\t\t\t\tconst error = createIdleTimeoutError(armMs);",
    ),
]
missing = [old[:60] for old, _ in edits if text.count(old) != 1]
if missing:
    print("skip openai-first-byte (dist shape changed): " + "; ".join(missing))
    raise SystemExit(0)
for old, new in edits:
    text = text.replace(old, new, 1)
path.write_text(text)
print("patched-openai-first-byte")
PY
        if grep -q 'RMP_OPENAI_FIRST_BYTE_20S' "$f" 2>/dev/null; then
            applied="${applied} openai-first-byte-20s"
        fi
    fi

    # Patch 7: 2026.9 session_nodes.entry_valid=0/-1 rows fail-closed the entire
    # store (every /hooks/agent). Keep placeholders skippable and allow parseable
    # pending rows through the canonical scan.
    if grep -q 'function scanCanonicalSqliteSessionEntries' "$f" 2>/dev/null \
       && ! grep -q 'RMP_SESSION_PLACEHOLDER_SKIP' "$f" 2>/dev/null; then
        python3 - "$f" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
old_scan = (
    '\t\tif (row.entry_json === "{}" && row.entry_valid === -1 && row.retained_window_id === row.current_session_id) continue;\n'
    '\t\tif (row.entry_valid !== 1) throw canonicalSessionKeyMigrationRequiredError(`invalid persisted session row requires repair for ${row.session_key}`);\n'
)
new_scan = (
    '\t\tif (row.entry_json === "{}" && row.entry_valid !== 1 && row.retained_window_id === row.current_session_id) continue; /* RMP_SESSION_PLACEHOLDER_SKIP */\n'
)
old_existing = (
    '\t\tif (existing && existing.entry_valid !== 1) {\n'
    '\t\t\tif (!(existing.entry_json === "{}" ? executeSqliteQueryTakeFirstSync(database.db, db.selectFrom("session_windows").select("session_id").where("session_id", "=", existing.current_session_id).where("session_key", "=", scope.sessionKey)) : void 0)) throw canonicalSessionKeyMigrationRequiredError(`invalid persisted session row requires repair for ${scope.sessionKey}`);\n'
    '\t\t}\n'
)
new_existing = (
    '\t\tif (existing && existing.entry_valid !== 1) {\n'
    '\t\t\tif (existing.entry_json !== "{}" && parseSessionEntryJson(existing)) { /* RMP_SESSION_PENDING_OK */ }\n'
    '\t\t\telse if (!(existing.entry_json === "{}" ? executeSqliteQueryTakeFirstSync(database.db, db.selectFrom("session_windows").select("session_id").where("session_id", "=", existing.current_session_id).where("session_key", "=", scope.sessionKey)) : void 0)) throw canonicalSessionKeyMigrationRequiredError(`invalid persisted session row requires repair for ${scope.sessionKey}`);\n'
    '\t\t}\n'
)
changed = False
if old_scan in text:
    text = text.replace(old_scan, new_scan, 1)
    changed = True
if old_existing in text:
    text = text.replace(old_existing, new_existing, 1)
    changed = True
if changed:
    path.write_text(text)
    print("patched-session-canonical")
PY
        if grep -q 'RMP_SESSION_PLACEHOLDER_SKIP' "$f" 2>/dev/null; then
            applied="${applied} session-canonical-lenient"
        fi
    fi

    # Patch 8: column updated_at can lag JSON updatedAt by tens of ms. The
    # stock parser then returns null and every hook throws SESSION_CANONICAL.
    if grep -q 'function parseSqliteSessionEntryRecord' "$f" 2>/dev/null \
       && ! grep -q 'RMP_SESSION_TS_DRIFT' "$f" 2>/dev/null; then
        python3 - "$f" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
old = (
    "\t\tif (row.current_session_id !== void 0 && row.current_session_id !== record.sessionId "
    "|| row.updated_at !== void 0 && row.updated_at !== record.updatedAt) return null;\n"
)
new = (
    "\t\tif (row.current_session_id !== void 0 && row.current_session_id !== record.sessionId) "
    "return null; /* RMP_SESSION_TS_DRIFT */\n"
)
if old in text:
    path.write_text(text.replace(old, new, 1))
    print("patched-session-ts-drift")
PY
        if grep -q 'RMP_SESSION_TS_DRIFT' "$f" 2>/dev/null; then
            applied="${applied} session-ts-drift"
        fi
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
echo "Note: model fallbacks left ENABLED (gpt-5-nano → gpt-oss-20b on NVIDIA)."

require_marker() {
    local pattern="$1"
    local label="$2"
    if ! grep -rqE "$pattern" "$DIST_DIR" --include='*.js' 2>/dev/null; then
        echo "ERROR: required patch missing after apply: $label"
        echo "  Dist symbols may have moved. Search $DIST_DIR and update this patcher."
        echo "  Do not start an unpatched gateway."
        exit 1
    fi
}

require_marker 'RMP_HOOK_PERSISTENCE' 'hook-persistence'
require_marker 'RMP_ANNOUNCE_SUPPRESS' 'announce-suppress'
require_marker 'RMP_MINIMAL_BOOTSTRAP' 'rmp-minimal-bootstrap'
require_marker '__RMP_SUPPRESS_NATIVE_SLACK' 'slack-rmp-suppress'
require_marker 'RMP_ALLOW_UNSAFE_EXTERNAL' 'allow-unsafe-passthrough'
require_marker 'RMP_FORCE_ALLOW_UNSAFE' 'allow-unsafe-rmp-force'
require_marker 'RMP_LLM_IDLE_5S' 'llm-idle-5s'
require_marker 'RMP_OPENAI_FIRST_BYTE_20S' 'openai-first-byte-20s'
require_marker 'RMP_410_SKIP' '410-skip-model-not-found'
require_marker 'RMP_SESSION_PLACEHOLDER_SKIP' 'session-canonical-placeholder-skip'
require_marker 'RMP_SESSION_TS_DRIFT' 'session-updatedAt-drift'

if [ "$PATCHED" -eq 0 ]; then
    echo "Already patched (idempotent re-run)."
fi
