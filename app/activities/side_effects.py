"""Idempotent side-effect wrappers for Slack and external HTTP."""
import asyncio
import hashlib
import logging
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import SideEffectReceipt

logger = logging.getLogger("rmp.side_effects")

SLACK_API_URL = "https://slack.com/api/chat.postMessage"
SLACK_PART_CHARS = 3500
TRANSIENT_SLACK_ERRORS = frozenset(
    {"ratelimited", "internal_error", "fatal_error", "service_unavailable", "request_timeout"}
)
INLINE_ATTEMPTS = 3


class SlackTransientError(RuntimeError):
    """Slack or the network failed for now; retrying later can deliver the message."""


def split_for_slack(message: str, limit: int = SLACK_PART_CHARS) -> List[str]:
    """Ordered parts of at most `limit` chars, cut at a paragraph, line, sentence or word break."""
    text = message.strip()
    parts: List[str] = []
    while len(text) > limit:
        window = text[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? ")) + 1
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


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


async def _post_part(client: httpx.AsyncClient, bot_token: str, user_id: str, text: str) -> Optional[str]:
    """None when Slack accepted the part; a permanent Slack error code otherwise.

    Transient failures are retried here a few times, then raised as SlackTransientError.
    """
    reason = ""
    for attempt in range(INLINE_ATTEMPTS):
        delay = 2.0 ** attempt
        try:
            resp = await client.post(
                SLACK_API_URL,
                headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
                json={"channel": user_id, "text": text},
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            reason = f"network: {exc}"
        else:
            if resp.status_code == 429 or resp.status_code >= 500:
                reason = f"http {resp.status_code}"
                retry_after = resp.headers.get("Retry-After", "")
                delay = min(float(retry_after), 30.0) if retry_after.isdigit() else delay
            else:
                try:
                    data = resp.json()
                except ValueError:
                    data = {"ok": False, "error": f"http {resp.status_code} non-json"}
                if data.get("ok"):
                    return None
                error = str(data.get("error") or "unknown_error")
                if error not in TRANSIENT_SLACK_ERRORS:
                    return error
                reason = error
        if attempt + 1 < INLINE_ATTEMPTS:
            await asyncio.sleep(delay)
    raise SlackTransientError(f"Slack delivery failed for now: {reason}")


async def _record_delivery_failure(task_id: str, user_id: str, error: str, part: int, parts: int) -> None:
    from app.db.models import Event
    from app.production.alerting import send_alert

    logger.error("Slack delivery failed for %s (part %d/%d): %s", task_id, part, parts, error)
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                Event(
                    correlation_id=task_id,
                    entity_type="task",
                    entity_id=task_id,
                    event_type="slack.delivery_failed",
                    event_payload={"user_id": user_id, "error": error, "part": part, "parts": parts},
                )
            )
            await db.commit()
    finally:
        await send_alert(
            "slack.delivery_failed",
            f"Slack rejected a reply for task {task_id[:8]}: {error}",
            severity="error",
            context={"task_id": task_id, "error": error},
        )


async def send_slack_message_idempotent(
    task_id: str,
    user_id: str,
    message: str,
    bot_token: str,
) -> bool:
    """Send a Slack DM once per (task_id, message), in ordered parts when it is long.

    Returns False on a permanent Slack error (recorded and alerted); raises
    SlackTransientError when delivery may still succeed later.
    """
    if not message or not user_id or not bot_token:
        return False

    idem_key = slack_idempotency_key(task_id, message)
    if await _already_sent(idem_key):
        logger.info("Slack send skipped (duplicate): %s", idem_key)
        return True

    parts = split_for_slack(message)
    async with httpx.AsyncClient() as client:
        for index, part in enumerate(parts, 1):
            part_key = slack_idempotency_key(task_id, f"{index}/{len(parts)}\n{part}")
            if len(parts) > 1 and await _already_sent(part_key):
                continue
            error = await _post_part(client, bot_token, user_id, part)
            if error:
                await _record_delivery_failure(task_id, user_id, error, index, len(parts))
                return False
            if len(parts) > 1:
                await _record_receipt(part_key, "slack.part", {"task_id": task_id, "part": index})

    await _record_receipt(
        idem_key, "slack", {"task_id": task_id, "user_id": user_id, "parts": len(parts)}
    )
    try:
        from app.db.models import Event, TaskMessage
        import uuid as _uuid

        async with AsyncSessionLocal() as db:
            db.add(
                Event(
                    correlation_id=task_id,
                    entity_type="task",
                    entity_id=task_id,
                    event_type="slack.delivered",
                    event_payload={"user_id": user_id, "parts": len(parts)},
                )
            )
            db.add(
                TaskMessage(
                    id=str(_uuid.uuid4()),
                    task_id=task_id,
                    role="assistant",
                    content=message,
                    source="slack",
                )
            )
            await db.commit()
    except Exception as exc:
        logger.warning("Slack ledger write failed for %s: %s", task_id, exc)
    return True
