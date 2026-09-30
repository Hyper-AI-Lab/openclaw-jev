"""Rebuild the patcher fixtures from pristine OpenClaw packages (npm pack, extracted).

For every dist file ``patch_openclaw.sh`` touches, the fixture keeps only the regions around the
code the patches target, so the tests run the real patcher on the real shapes of each release.

    python3 tests/fixtures/openclaw_dist/build.py 2026.9.1=/tmp/oc-dists/2026.9.1/package/dist ...
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MINIFIED_LINE = 3000
FUNCTION_LINES = 400
# Patch targets, and how much context each needs: its enclosing function or a line window.
TARGETS = [
    (r'hookRunner\?\.hasHooks\("before_message_write"\)', 6),
    (r"async function runSubagentAnnounceFlow\(params\) \{", 6),
    (r"function filterBootstrapFilesForSession\(files, \w+\) \{", 6),
    (r"async function deliverReplies(\$[0-9]+)?\(params\) \{", 6),
    (r"function normalizeAgentPayload\(payload\)", "function"),
    (r"allowUnsafeExternalContent: value\.allowUnsafeExternalContent,", 6),
    (r"DEFAULT_LLM_IDLE_TIMEOUT_MS = 12e4;", 6),
    (r"\tif \(status === 410\) \{\n\t\tif \(messageReason === \"session_expired\"", 8),
    (r"function resolveLlmFirstEventTimeoutMs\(params\)", "function"),
    (r"function streamWithIdleTimeout\(baseFn, timeoutMs, onIdleTimeout, opts\) \{", "function"),
    (r"firstEventTimeoutMs: optionsWithFirstEvent\?\.firstEventTimeoutMs \?\? firstEventTimeoutMs,", 6),
    (r"function buildOpenAIThinkingProfile\(params\) \{", "function"),
    (r"OPENAI_GPT_6_MODEL_IDS", 1),
    (r"resolveOpenAIResponsesPayloadPolicy\(model, \{\n\t\t\textraParams,", 8),
    (r"if \(row\.entry_valid !== 1\) throw canonicalSessionKeyMigrationRequiredError", 6),
    (r"function validateCanonicalSessionRow\(row, mode = \"admission\"\) \{", "function"),
    (r"function validateCanonicalSessionRowEntry\(row, entry, mode = \"admission\"\) \{", 6),
    (r"if \(existing && existing\.entry_valid !== 1\) \{", 6),
    (r"function parseSqliteSessionEntryRecord\(", "function"),
    (r"\|\| row\.updated_at !== void 0 && row\.updated_at !== record\.updatedAt\) return null;", 4),
    (r"for \(const placement of placements\.list\(\)\) try \{\n\t\t\tconst root = await deps\.resolveWorkspacePath", 12),
    (r"cleanedWorkspaceRoots = ", 2),
]


def regions(text: str):
    lines = text.split("\n")
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)

    def line_of(offset):
        lo, hi = 0, len(lines)
        while lo < hi:
            mid = (lo + hi) // 2
            if starts[mid + 1] <= offset:
                lo = mid + 1
            else:
                hi = mid
        return lo

    spans = []
    for pattern, context in TARGETS:
        for m in re.finditer(pattern, text):
            first, last = line_of(m.start()), line_of(m.end())
            # Minified bundles hold some of the same functions but are never patched.
            if len(lines[first]) > MINIFIED_LINE:
                continue
            if context == "function":
                end = last
                while end < min(len(lines) - 1, first + FUNCTION_LINES) and lines[end] != "}":
                    end += 1
                spans.append((max(0, first - 1), end))
            else:
                spans.append((max(0, first - context), min(len(lines) - 1, last + context)))
    spans.sort()
    merged = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return ["\n".join(lines[lo:hi + 1]) for lo, hi in merged]


def build(version: str, dist: Path) -> None:
    out = HERE / version
    for path in sorted(dist.rglob("*")):
        if path.suffix not in (".js", ".mjs") or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not any(re.search(pattern, text) for pattern, _ in TARGETS):
            continue
        parts = regions(text)
        if not parts:
            continue
        target = out / path.relative_to(dist)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n\n// ---- fixture cut ----\n\n".join(parts) + "\n", encoding="utf-8")


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        version, _, dist = arg.partition("=")
        build(version, Path(dist))
