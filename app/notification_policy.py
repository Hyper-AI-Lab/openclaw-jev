"""Rules for when RMP should deliver messages to Slack."""
from __future__ import annotations

import re
from typing import Iterable, List, Optional

from app.orchestrator.step_predicates import _strip_json_blocks, extract_agent_facts

INTERNAL_TAGS = frozenset({"canary", "system", "heartbeat", "intake-smoke"})
SILENT_ACKS = frozenset({"HEARTBEAT_OK", "CANARY_OK"})
_ACK_PREFIX_RE = re.compile(
    r"^(\s*(?:HEARTBEAT_OK|CANARY_OK)\s*)+",
    re.IGNORECASE,
)


def _dedupe_repeated_body(text: str) -> str:
    """Collapse duplicate paragraphs or exact duplicate halves."""
    cleaned = (text or "").strip()
    parts = [p.strip() for p in re.split(r"\n{2,}", cleaned) if p.strip()]
    if len(parts) >= 2 and len(set(parts)) == 1:
        return parts[0]
    if len(cleaned) < 40:
        return cleaned
    mid = len(cleaned) // 2
    for offset in (0, 1):
        pivot = mid + offset
        first = cleaned[:pivot].strip()
        second = cleaned[pivot:].strip()
        if first and first == second:
            return first
    return cleaned


def sanitize_user_facing_text(message: str) -> str:
    """Strip machine JSON, internal metadata, and duplicate bodies before Slack."""
    text = strip_system_acks(message or "")
    text = extract_agent_facts(text).get("body") or text
    text = _strip_json_blocks(text)
    text = re.sub(r"```json\s*\{[\s\S]*?\}\s*```", "", text, flags=re.IGNORECASE)
    for pattern in (
        r"(?m)^Origin:\s.*$",
        r"(?m)^Session:\s.*$",
        r"(?m)^Timestamp:\s.*$",
        r"(?m)^Model:\s.*$",
        r"(?m)^RMP integration:\s.*$",
        r"(?m)^Memory status:\s.*$",
        r"(?m)^Goal:\s.*$",
        r"(?m)^Emotional state:\s.*$",
        r"(?m)^EOF\s*$",
    ):
        text = re.sub(pattern, "", text)
    text = re.sub(r"\[INTERNAL_RMP\]", "", text)
    text = re.sub(r"\[RMP_DELIVER\]", "", text)
    text = _dedupe_repeated_body(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def strip_system_acks(message: str) -> str:
    """Remove leading HEARTBEAT_OK / CANARY_OK tokens from user-facing text."""
    text = (message or "").strip()
    text = re.sub(r"\[\[reply_to_current\]\]", "", text, flags=re.IGNORECASE).strip()
    while True:
        match = _ACK_PREFIX_RE.match(text)
        if not match:
            break
        text = text[match.end() :].strip()
    return text


def is_heartbeat_request(text: str) -> bool:
    t = (text or "").lower()
    return "heartbeat.md" in t or (
        t.strip().startswith("[cron:") and "heartbeat" in t
    )


def is_smoke_test_intent(intent: str) -> bool:
    """Phase 5/6 intake verification tasks — not user work."""
    lower = (intent or "").lower()
    return (
        "intake attach smoke" in lower
        or "intake live canary" in lower
        or "intake smoke" in lower
    )


def is_canary_intent(intent: str) -> bool:
    upper = (intent or "").upper()
    return "RMP CANARY" in upper or "RMP MEMORY CANARY" in upper


def is_internal_intent(intent: str) -> bool:
    return is_canary_intent(intent) or is_smoke_test_intent(intent) or is_heartbeat_request(intent)


def is_internal_task(
    intent: str,
    task_type: str = "",
    tags: Optional[Iterable[str]] = None,
) -> bool:
    tag_set = {t.lower() for t in (tags or [])}
    if tag_set & INTERNAL_TAGS:
        return True
    if (task_type or "").lower() in ("heartbeat", "canary"):
        return True
    if is_internal_intent(intent):
        return True
    return False


def is_silent_system_ack(text: str) -> bool:
    normalized = (text or "").strip()
    if not normalized:
        return False
    first_line = normalized.split("\n")[0].strip()
    if first_line in SILENT_ACKS or normalized in SILENT_ACKS:
        return True
    # Allow "CANARY_OK" with trailing task_status JSON stripped elsewhere.
    if first_line.startswith("CANARY_OK") and len(first_line) <= 20:
        return True
    return False


def is_system_slack_message(message: str) -> bool:
    cleaned = (message or "").strip()
    if not cleaned:
        return True
    if is_silent_system_ack(cleaned):
        return True
    first_line = cleaned.split("\n")[0].strip()
    if re.match(r"^\[\d{4}-\d{2}-\d{2} .* UTC\] Hook$", first_line):
        return True
    return False


def should_deliver_slack(
    intent: str,
    task_type: str = "",
    tags: Optional[Iterable[str]] = None,
    message: str = "",
) -> bool:
    if is_internal_task(intent, task_type, tags):
        return False
    if message and is_system_slack_message(message):
        return False
    return True


def format_workflow_error(exc: Exception) -> str:
    """Turn Temporal/activity wrappers into user-readable Slack text."""
    parts: List[str] = []
    seen = set()
    current: Optional[BaseException] = exc
    depth = 0
    while current is not None and depth < 6:
        text = str(current).strip()
        if text and text not in seen:
            parts.append(text)
            seen.add(text)
        current = getattr(current, "cause", None) or current.__cause__
        depth += 1

    blob = " ".join(parts).lower()

    if "timed out waiting for agent reply after rate-limit" in blob:
        return (
            "Aura is waiting for the NVIDIA API rate limit to clear. "
            "Rotating keys and retrying — no action needed."
        )
    if "timed out waiting for agent reply" in blob:
        return (
            "Aura did not finish in time. The system is still retrying."
        )
    if "agent stopped without a usable reply" in blob:
        return (
            "Aura hit a snag finishing that turn (empty reply after tools). Retrying now."
        )
    if "rate limit" in blob or "429" in blob:
        return (
            "NVIDIA API rate limit reached. Rotating keys and waiting briefly before retrying."
        )
    if "400 status code" in blob:
        return (
            "The NVIDIA API rejected the request (400). Waiting before retrying."
        )
    if "activity task failed" in blob and len(parts) > 1:
        return parts[-1][:200]
    if parts:
        return parts[-1][:200]
    return "An unexpected error occurred."
