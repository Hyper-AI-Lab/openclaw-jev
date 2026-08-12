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
    """Return latest terminal assistant reply for an RMP task session, if any."""
    session_key = f"agent:main:rmp_task_{task_id}"
    session_id = None
    try:
        if os.path.exists(SESSIONS_JSON_PATH):
            with open(SESSIONS_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            meta = data.get(session_key) or {}
            session_id = meta.get("sessionId")
            session_file = meta.get("sessionFile")
            if session_file and Path(session_file).exists():
                text = _latest_assistant_text(Path(session_file))
                if text and _is_terminal_reply(text):
                    return text
    except Exception as exc:
        logger.debug("sessions.json lookup failed for %s: %s", task_id, exc)

    if session_id:
        path = Path(OPENCLAW_HOME) / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
        text = _latest_assistant_text(path)
        if text and _is_terminal_reply(text):
            return text
    return None


def extract_user_facing_reply(raw: str) -> str:
    """Strip facts/task_status fences for Slack delivery."""
    cleaned = sanitize_user_facing_text(raw or "")
    cleaned = _FACTS_RE.sub("", cleaned).strip()
    return cleaned
