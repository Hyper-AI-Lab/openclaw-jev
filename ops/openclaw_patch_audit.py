"""Which of RMP's OpenClaw dist patches are in place, and which code they target is still unpatched.

The single list of RMP's patches, shared by ``patch_openclaw.sh`` (after applying them) and
``ops/verify_openclaw_patch.sh``. Each patch has a marker it leaves in the dist and the exact
code it rewrites. A dist passes when every required marker is present and none of that code
remains in any ``.js`` or ``.mjs`` file. That catches a half-patched dist: 2026.9.7 repeats most
targets in ``package-update-activation-recovery.mjs``, and a patch landing in one copy only
must fail. Minified worker bundles carry some of the same functions in a shape no exact edit
matches, and they never match the unpatched code here either.

A patch whose target code a release no longer has is required only where it applies: the GPT-6
thinking backport is satisfied by releases that ship GPT-6 (``OPENAI_GPT_6_MODEL_IDS``), and
the local placement cleanup skip applies only where the old cleanup loop exists (2026.9.1;
2026.9.7 reworked startup placement work).

    python3 ops/openclaw_patch_audit.py [DIST_DIR]
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

DEFAULT_DIST = Path("/usr/lib/node_modules/openclaw/dist")


@dataclass(frozen=True)
class Patch:
    label: str
    marker: str
    unpatched: Sequence[str]
    # Required only when the dist has this code (None: always required).
    applies_when: Optional[str] = None


PATCHES: Sequence[Patch] = (
    Patch("hook-persistence", r"RMP_HOOK_PERSISTENCE", [r'hookRunner\?\.hasHooks\("before_message_write"\)']),
    Patch("announce-suppress", r"RMP_ANNOUNCE_SUPPRESS",
          [r"async function runSubagentAnnounceFlow\(params\) \{(?! if \(params\.childSessionKey)"]),
    Patch("rmp-minimal-bootstrap", r"RMP_MINIMAL_BOOTSTRAP",
          [r"function filterBootstrapFilesForSession\(files, \w+\) \{(?! const __rmpSk)"]),
    Patch("slack-rmp-suppress", r"__RMP_SUPPRESS_NATIVE_SLACK",
          [r"async function deliverReplies(\$[0-9]+)?\(params\) \{(?! try \{ if \(typeof globalThis\.__RMP_SUPPRESS_NATIVE_SLACK)"]),
    Patch("allow-unsafe-passthrough", r"RMP_ALLOW_UNSAFE_EXTERNAL",
          [r"function normalizeAgentPayload\(payload\)(?:(?!\nfunction )[\s\S]){0,4000}?\t\t\tmodel,\n\t\t\tthinking,\n\t\t\ttimeoutSeconds\n\t\t\}",
           r"timeoutSeconds: typeof timeoutRaw === \"number\" && Number\.isFinite\(timeoutRaw\) && timeoutRaw > 0 \? Math\.floor\(timeoutRaw\) : void 0\n\t\t\}"]),
    Patch("allow-unsafe-rmp-force", r"RMP_FORCE_ALLOW_UNSAFE",
          [r"allowUnsafeExternalContent: value\.allowUnsafeExternalContent,"]),
    # A const in most chunks, an assignment to a hoisted var in 2026.9.7's recovery bundle.
    Patch("llm-idle-5s", r"RMP_LLM_IDLE_5S", [r"\bDEFAULT_LLM_IDLE_TIMEOUT_MS = 12e4;"]),
    Patch("410-skip-model-not-found", r"RMP_410_SKIP",
          [r"\tif \(status === 410\) \{\n\t\tif \(messageReason === \"session_expired\" [^\n]*\n\t\treturn toReasonClassification\(\"timeout\"\);"]),
    Patch("openai-first-byte-20s", r"RMP_OPENAI_FIRST_BYTE_20S",
          [r"isSelfHostedRuntimeModel \? LOCAL_LLM_FIRST_EVENT_TIMEOUT_MS : CLOUD_LLM_FIRST_EVENT_TIMEOUT_MS, \.\.\.timeoutBounds\)\);",
           r"\t\tconst createIdleTimeoutError = \(\) => /\* @__PURE__ \*/ new Error\(`LLM idle timeout \(\$\{Math\.floor\(timeoutMs / 1e3\)\}s\)"]),
    Patch("openai-max-effort-120s", r"RMP_OPENAI_MAX_EFFORT_120S",
          [r"firstEventTimeoutMs: optionsWithFirstEvent\?\.firstEventTimeoutMs \?\? firstEventTimeoutMs,",
           r"const armMs = isFirstStreamArm \? firstByteMs : timeoutMs;"]),
    Patch("gpt6-thinking-levels", r"RMP_GPT6_THINKING_BACKPORT|OPENAI_GPT_6_MODEL_IDS",
          [r"\tconst codexEfforts = params\.compat\?\.supportedReasoningEfforts\?\.map\(normalizeLowercaseStringOrEmpty\);\n(?!\tif \(/\^gpt-6)"]),
    Patch("openai-no-store", r"RMP_OPENAI_NO_STORE",
          [r"resolveOpenAIResponsesPayloadPolicy\(model, \{\n\t\t\textraParams,\n\t\t\tenablePromptCacheStripping: true,\n\t\t\tenableServerCompaction: true,"]),
    Patch("session-canonical-placeholder-skip", r"RMP_SESSION_PLACEHOLDER_SKIP",
          [r"if \(row\.entry_valid !== 1\) throw canonicalSessionKeyMigrationRequiredError",
           r'if \(row\.entry_json === "\{\}" && row\.entry_valid === -1 && row\.retained_window_id === row\.current_session_id\) (?:continue|return);',
           r'row\.entry_valid !== 1 && \(mode !== "read" \|\| row\.entry_valid !== 0\)\) throw',
           r'row\.entry_valid === 1 \|\| mode === "read" && row\.entry_valid === 0 \? parseSqliteSessionEntryRecord',
           r"if \(existing && existing\.entry_valid !== 1\) \{\n\t\t\tif \(!\(existing\.entry_json"]),
    Patch("session-updatedAt-drift", r"RMP_SESSION_TS_DRIFT",
          [r"\|\| row\.updated_at !== void 0 && row\.updated_at !== record\.updatedAt\) return null;"]),
    Patch("skip-local-placement-cleanup", r"RMP_SKIP_LOCAL_PLACEMENT_CLEANUP",
          [r"for \(const placement of placements\.list\(\)\) try \{\n\t\t\tconst root = await deps\.resolveWorkspacePath\(placement\);"],
          applies_when=r"cleanedWorkspaceRoots = "),
)


def dist_texts(dist: Path) -> Dict[Path, str]:
    return {path: path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(dist.rglob("*")) if path.suffix in (".js", ".mjs") and path.is_file()}


def audit(dist: Path, patches: Iterable[Patch] = PATCHES) -> List[str]:
    """Failures, one line each; empty when the dist is fully patched."""
    if not dist.is_dir():
        return [f"OpenClaw dist not found at {dist}"]
    texts = dist_texts(dist)
    failures: List[str] = []
    for patch in patches:
        if patch.applies_when and not any(re.search(patch.applies_when, t) for t in texts.values()):
            continue
        if not any(re.search(patch.marker, t) for t in texts.values()):
            failures.append(f"required patch missing after apply: {patch.label}")
        rxs = [re.compile(pattern) for pattern in patch.unpatched]
        for path, text in texts.items():
            if any(rx.search(text) for rx in rxs):
                failures.append(f"unpatched code left for {patch.label}: {path.relative_to(dist)}")
    return failures


def main(argv: Sequence[str]) -> int:
    dist = Path(argv[0]) if argv else DEFAULT_DIST
    failures = audit(dist)
    for failure in failures:
        print(f"ERROR: {failure}")
    if failures:
        print(f"  Dist symbols may have moved. Search {dist} and update patch_openclaw.sh.")
        print("  Do not start an unpatched gateway.")
        return 1
    print(f"OK: all {len(PATCHES)} RMP patches in place under {dist}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
