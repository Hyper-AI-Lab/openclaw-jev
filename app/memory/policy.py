"""Memory write policy: secret redaction and scope gates by memory type."""
import re
from typing import Dict, FrozenSet, Tuple

SECRET_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "[REDACTED:api_key]"),
    (re.compile(r"xox[baprs]-[a-zA-Z0-9-]+"), "[REDACTED:slack_token]"),
    (re.compile(r"ghp_[a-zA-Z0-9]{20,}"), "[REDACTED:github_token]"),
    (re.compile(r"Bearer\s+[a-zA-Z0-9._-]+", re.I), "Bearer [REDACTED]"),
    (
        re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+"),
        "[REDACTED:secret]",
    ),
]

SCOPE_GATES: Dict[str, FrozenSet[str]] = {
    "working": frozenset({"process", "task"}),
    "semantic": frozenset({"process", "task", "user"}),
    "episodic": frozenset({"process", "task", "user"}),
    "procedural": frozenset({"procedural"}),
    "pinned": frozenset({"user"}),
}

MIN_CONFIDENCE = 1
MAX_CONFIDENCE = 100


def redact_secrets(content: str) -> str:
    if not content:
        return content
    redacted = content
    for pattern, replacement in SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def is_scope_allowed(scope_type: str, memory_type: str) -> bool:
    allowed = SCOPE_GATES.get(memory_type)
    if allowed is None:
        return True
    return scope_type in allowed


def apply_write_policy(
    scope_type: str,
    memory_type: str,
    content: str,
    confidence: int = 100,
) -> Tuple[bool, str, str]:
    """Return (allowed, reason, redacted_content)."""
    if not content or not str(content).strip():
        return False, "empty_content", ""

    if confidence < MIN_CONFIDENCE or confidence > MAX_CONFIDENCE:
        return False, "invalid_confidence", redact_secrets(content)

    if not is_scope_allowed(scope_type, memory_type):
        return False, f"scope_{scope_type}_not_allowed_for_{memory_type}", redact_secrets(content)

    return True, "ok", redact_secrets(content)
