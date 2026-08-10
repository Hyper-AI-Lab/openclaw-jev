"""Direct Slack alerts for production ops — bypasses internal-task suppression."""
from __future__ import annotations

import logging
from typing import Optional

from app.activities.openclaw_activities import _get_slack_user_id
from app.activities.side_effects import send_slack_message_idempotent
from app.config import get_slack_bot_token, load_settings

logger = logging.getLogger("rmp.ops_notify")


def get_ops_slack_config() -> dict:
    settings = load_settings()
    prod = settings.get("production", {})
    cfg = prod.get("ops_slack", {})
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "session_key": cfg.get("session_key", "agent:main:main"),
    }


async def notify_ops_slack(message: str, *, incident_id: str) -> bool:
    """Send a one-off ops alert to the operator (deduped by incident_id + message)."""
    cfg = get_ops_slack_config()
    if not cfg["enabled"]:
        logger.info("Ops Slack disabled — would alert: %s", message[:120])
        return False

    bot_token = get_slack_bot_token()
    user_id = _get_slack_user_id(cfg["session_key"])
    if not bot_token or not user_id:
        logger.warning("Ops Slack skipped: token=%s user=%s", bool(bot_token), bool(user_id))
        return False

    text = (message or "").strip()
    if not text:
        return False

    return await send_slack_message_idempotent(
        task_id=f"ops:{incident_id}",
        user_id=user_id,
        message=text,
        bot_token=bot_token,
    )
