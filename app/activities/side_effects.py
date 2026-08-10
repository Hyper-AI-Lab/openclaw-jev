"""Idempotent side-effect wrappers for Slack and external HTTP."""
import hashlib
import logging
from typing import Any, Dict, Optional

import httpx
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import SideEffectReceipt

logger = logging.getLogger("rmp.side_effects")

SLACK_API_URL = "https://slack.com/api/chat.postMessage"


def slack_idempotency_key(task_id: str, message: str) -> str:
    msg_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]
    return f"slack:{task_id}:{msg_hash}"


async def _already_sent(idempotency_key: str) -> bool:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SideEffectReceipt).where(
                SideEffectReceipt.idempotency_key == idempotency_key
            )
        )
        return result.scalar_one_or_none() is not None


async def _record_receipt(
    idempotency_key: str, effect_type: str, metadata: Optional[Dict[str, Any]] = None
) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SideEffectReceipt).where(
                SideEffectReceipt.idempotency_key == idempotency_key
            )
        )
        if result.scalar_one_or_none():
            return
        db.add(
            SideEffectReceipt(
                idempotency_key=idempotency_key,
                effect_type=effect_type,
                metadata_ref=metadata or {},
            )
        )
        await db.commit()


async def send_slack_message_idempotent(
    task_id: str,
    user_id: str,
    message: str,
    bot_token: str,
) -> bool:
    """Send Slack DM once per (task_id, message) pair."""
    if not message or not user_id or not bot_token:
        return False

    idem_key = slack_idempotency_key(task_id, message)
    if await _already_sent(idem_key):
        logger.info("Slack send skipped (duplicate): %s", idem_key)
        return True

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                SLACK_API_URL,
                headers={
                    "Authorization": f"Bearer {bot_token}",
                    "Content-Type": "application/json",
                },
                json={"channel": user_id, "text": message[:4000]},
                timeout=15.0,
            )
            data = resp.json()
            if data.get("ok"):
                await _record_receipt(
                    idem_key,
                    "slack",
                    {"task_id": task_id, "user_id": user_id},
                )
                return True
            logger.warning("Slack API error: %s", data.get("error"))
            return False
        except Exception as e:
            logger.warning("Slack post failed: %s", e)
            return False
