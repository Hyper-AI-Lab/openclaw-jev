"""RMP-owned Slack notices without starting Aura.

Used when POST /tasks cannot complete after recovery, or a whole-message
stop arrives with no active user task. Never a native OpenClaw reply.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict

from app.activities.openclaw_activities import _get_slack_user_id
from app.activities.side_effects import SlackTransientError, send_slack_message_idempotent
from app.config import get_slack_bot_token, should_suspend_slack
from app.notification_policy import should_deliver_slack

logger = logging.getLogger("rmp.notify_user")

ALLOWED_REASONS = frozenset({"intake_unavailable", "stop_idle"})

NOTICE_TEXT = {
    "intake_unavailable": (
        "I received that, but I could not start work just now. "
        "Please try again in a moment."
    ),
    "stop_idle": "Nothing is running to stop.",
}


def notice_task_id(session_key: str, reason: str, idempotency_key: str) -> str:
    raw = f"{session_key}:{reason}:{idempotency_key or NOTICE_TEXT[reason]}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"notify:{reason}:{digest}"


async def deliver_user_notice(
    *,
    session_key: str,
    reason: str,
    idempotency_key: str = "",
) -> Dict[str, Any]:
    if reason not in ALLOWED_REASONS:
        return {"ok": False, "delivered": False, "error": "unknown_reason"}
    if should_suspend_slack():
        return {"ok": True, "delivered": False, "reason": "slack_suspended"}

    message = NOTICE_TEXT[reason]
    if not should_deliver_slack("", "user", ["user-request"], message):
        return {"ok": True, "delivered": False, "reason": "suppressed"}

    bot_token = get_slack_bot_token()
    user_id = _get_slack_user_id(session_key or "")
    if not bot_token or not user_id:
        logger.warning(
            "User notice skipped: token=%s user=%s", bool(bot_token), bool(user_id)
        )
        return {"ok": True, "delivered": False, "reason": "missing_slack_config"}

    task_id = notice_task_id(session_key or "unknown", reason, idempotency_key)
    try:
        delivered = await send_slack_message_idempotent(
            task_id=task_id,
            user_id=user_id,
            message=message,
            bot_token=bot_token,
        )
    except SlackTransientError as exc:
        logger.warning("User notice not delivered yet (%s): %s", reason, exc)
        delivered = False
    return {
        "ok": True,
        "delivered": bool(delivered),
        "task_id": task_id,
        "reason": reason,
    }
