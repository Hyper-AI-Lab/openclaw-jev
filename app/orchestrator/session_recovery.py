"""Recover Slack delivery when OpenClaw finished but Temporal/worker restarted."""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from app.config import OPENCLAW_HOME, SESSIONS_JSON_PATH
from app.notification_policy import sanitize_user_facing_text
from app.openclaw_sessions import get_session_entry, read_transcript_lines, task_session_keys

logger = logging.getLogger("rmp.session_recovery")

_FACTS_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def _is_terminal_reply(text: str) -> bool:
    cleaned = (text or "").strip()
    if len(cleaned) < 10:
        return False
    if re.search(r'"facts"\s*:', cleaned) or re.search(r'"task_status"\s*:', cleaned):
        return True
    return len(cleaned) >= 80


def _latest_assistant_text(jsonl_path: Path) -> Optional[str]:
    if not jsonl_path.exists():
        return None
    best: Optional[str] = None
    try:
        for line in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") != "message":
                continue
            msg = obj.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            parts = msg.get("content") or []
            texts = []
            for p in parts:
                if isinstance(p, dict) and p.get("type") == "text":
                    texts.append(p.get("text") or "")
            text = "\n".join(texts).strip()
            if not text:
                continue
            if "[assistant turn failed" in text.lower():
                continue
            best = text
    except Exception as exc:
        logger.warning("Failed reading session jsonl %s: %s", jsonl_path, exc)
        return None
    return best


def read_completed_rmp_session_reply(task_id: str) -> Optional[str]:
    """The latest terminal assistant reply across the task's sessions (reworks run in their own)."""
    latest = None
    for session_key in task_session_keys(task_id):
        text = _session_reply(session_key, task_id)
        if text:
            latest = text
    return latest


def _session_reply(session_key: str, task_id: str) -> Optional[str]:
    session_id = None
    try:
        meta = get_session_entry(session_key, SESSIONS_JSON_PATH)
        session_id = meta.get("sessionId")
        session_file = meta.get("sessionFile")
        if session_file and Path(session_file).exists():
            text = _latest_assistant_text(Path(session_file))
            if text and _is_terminal_reply(text):
                return text
    except Exception as exc:
        logger.debug("session lookup failed for %s: %s", task_id, exc)

    if session_id:
        path = Path(OPENCLAW_HOME) / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
        text = _latest_assistant_text(path)
        if text and _is_terminal_reply(text):
            return text
        lines = read_transcript_lines(session_id)
        if lines:
            best = None
            for line in lines:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "message":
                    continue
                msg = obj.get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                parts = msg.get("content") or []
                texts = []
                for p in parts:
                    if isinstance(p, dict) and p.get("type") == "text":
                        texts.append(p.get("text") or "")
                text = "\n".join(texts).strip()
                if text and "[assistant turn failed" not in text.lower():
                    best = text
            if best and _is_terminal_reply(best):
                return best
    return None


def extract_user_facing_reply(raw: str) -> str:
    """Strip facts/task_status fences for Slack delivery."""
    cleaned = sanitize_user_facing_text(raw or "")
    cleaned = _FACTS_RE.sub("", cleaned).strip()
    return cleaned
