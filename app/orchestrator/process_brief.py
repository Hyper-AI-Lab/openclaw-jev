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


def format_user_catchup(messages: Iterable[str]) -> str:
    chunks = [m.strip() for m in messages if (m or "").strip()]
    if not chunks:
        return ""
    body = "\n---\n".join(chunks)
    return f"USER CATCH-UP (attach/rebuild — follow this plus the original ask):\n{body}"
