"""Compose PROCESS BRIEF + process memory for Aura executor prompts."""
from __future__ import annotations

from typing import Iterable, Optional


def ensure_brief_header(block: str) -> str:
    text = (block or "").strip()
    if not text:
        return ""
    if "PROCESS BRIEF" in text[:400]:
        return text
    return f"PROCESS BRIEF:\n{text}"


def compose_executor_memory(*parts: Optional[str]) -> str:
    """Dedupe-preserving join of brief + fetched process memory."""
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = (part or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return "\n\n".join(out)


USER_MESSAGE_MARKER = "\n\nUSER MESSAGE:\n"


def with_catchup(catchup: str, message: str) -> str:
    """What intake signals a running task: the task's brief, then Kirill's message."""
    return f"{catchup}{USER_MESSAGE_MARKER}{message}" if catchup else message


def user_words(signalled: str) -> str:
    """Kirill's message from a signal built by with_catchup."""
    _, marker, message = signalled.partition(USER_MESSAGE_MARKER)
    return (message if marker else signalled).strip()


def format_user_catchup(messages: Iterable[str]) -> str:
    chunks = [m.strip() for m in messages if (m or "").strip()]
    if not chunks:
        return ""
    body = "\n---\n".join(chunks)
    return f"USER CATCH-UP (attach/rebuild — follow this plus the original ask):\n{body}"
