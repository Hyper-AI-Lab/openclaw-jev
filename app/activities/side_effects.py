"""Idempotent side-effect wrappers for Slack and external HTTP."""
import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


async def _post_part(
    client: httpx.AsyncClient, bot_token: str, user_id: str, text: str
) -> Tuple[Optional[str], Optional[str]]:
    """(None, message ts) when Slack accepted the part; (permanent Slack error code, None) otherwise.

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
                    return None, data.get("ts")
                error = str(data.get("error") or "unknown_error")
                if error not in TRANSIENT_SLACK_ERRORS:
                    return error, None
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
    *,
    kind: str = "notice",
    session_key: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> bool:
    """Send a Slack DM once per (task_id, message), in ordered parts when it is long.

    ``kind`` is how the conversation log records it: ``reply`` (Aura's judged answer),
    ``followup`` or ``notice`` (RMP's own text). A task_id with no tasks row, as an RMP notice
    has, is recorded as a ``slack.delivered`` event only, with no conversation message. Returns
    False on a permanent Slack error (recorded and alerted); raises SlackTransientError when
    delivery may still succeed later.
    """
    if not message or not user_id or not bot_token:
        return False

    idem_key = slack_idempotency_key(task_id, message)
    if await _already_sent(idem_key):
        logger.info("Slack send skipped (duplicate): %s", idem_key)
        return True

    parts = split_for_slack(message)
    first_ts: Optional[str] = None
    async with httpx.AsyncClient() as client:
        for index, part in enumerate(parts, 1):
            part_key = slack_idempotency_key(task_id, f"{index}/{len(parts)}\n{part}")
            if len(parts) > 1 and await _already_sent(part_key):
                continue
            error, ts = await _post_part(client, bot_token, user_id, part)
            if error:
                await _record_delivery_failure(task_id, user_id, error, index, len(parts))
                return False
            first_ts = first_ts or ts
            if len(parts) > 1:
                await _record_receipt(
                    part_key, "slack.part", {"task_id": task_id, "part": index, "ts": ts}
                )

    await _record_receipt(
        idem_key,
        "slack",
        {"task_id": task_id, "user_id": user_id, "parts": len(parts), "ts": first_ts},
    )
    try:
        from app.db.models import Event, Task, TaskMessage
        from app.deep_memory.ingest import TURN_KINDS, enqueue
        import uuid as _uuid

        message_id = str(_uuid.uuid4())
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
            # RMP's own notices (ops alerts, user notices) have an id but no tasks row, so a message
            # row would fail its foreign key and roll the delivery event back with it.
            if await db.get(Task, task_id) is not None:
                db.add(
                    TaskMessage(
                        id=message_id,
                        task_id=task_id,
                        role="assistant",
                        content=message,
                        source="slack",
                        slack_ts=first_ts,
                        kind=kind,
                        session_key=session_key or None,
                        meta={**(meta or {}), "parts": len(parts)},
                    )
                )
                if kind in TURN_KINDS:
                    await db.flush()
                    await enqueue(db, "turn", message_id)
            await db.commit()
    except Exception as exc:
        logger.warning("Slack ledger write failed for %s: %s", task_id, exc)
    return True


SLACK_API = "https://slack.com/api"


class SlackFileError(RuntimeError):
    """Slack refused a file for good; retrying won't send it."""


def _file_key(task_id: str, file_id: str) -> str:
    return f"slack-file:{task_id}:{file_id}"


async def pending_reply_files(task_id: str) -> List[Dict[str, Any]]:
    """The files Aura attached in the task that RMP has neither sent nor refused, oldest first."""
    from app.db.models import Event

    async with AsyncSessionLocal() as db:
        attached = (await db.execute(
            select(Event).where(Event.entity_id == task_id, Event.event_type == "reply.file_attached")
            .order_by(Event.occurred_at)
        )).scalars().all()
        keys = {_file_key(task_id, e.event_payload["id"]) for e in attached}
        done = set((await db.execute(
            select(SideEffectReceipt.idempotency_key).where(SideEffectReceipt.idempotency_key.in_(keys))
        )).scalars().all()) if keys else set()
    return [e.event_payload for e in attached if _file_key(task_id, e.event_payload["id"]) not in done]


async def send_reply_files(task_id: str, user_id: str, bot_token: str) -> List[str]:
    """Upload the files Aura attached into Kirill's DM, each once; the names sent.

    A file that changed or fails RMP's checks since it was attached, or that Slack refuses, is not sent, and
    Kirill gets a notice saying so. Slack or the network failing for now raises SlackTransientError.
    """
    from app.coding import outbox

    files = await pending_reply_files(task_id)
    if not files:
        return []
    sent = []
    async with httpx.AsyncClient() as client:
        channel = (await _slack_call(client, bot_token, "conversations.open", {"users": user_id}))["channel"]["id"]
        for attached in files:
            key = _file_key(task_id, attached["id"])
            try:
                now = await asyncio.to_thread(outbox.check, task_id, attached["path"])
                if now["sha256"] != attached["sha256"]:
                    raise outbox.FileRefused(f"{attached['name']} changed after Aura attached it")
                slack_file = await _upload(client, bot_token, channel, Path(now["path"]), attached["title"])
            except (outbox.FileRefused, SlackFileError) as exc:
                await _record_receipt(key, "slack.file_refused", {"task_id": task_id, "error": str(exc)})
                await _file_event(task_id, "reply.file_refused", {"id": attached["id"], "name": attached["name"],
                                                                  "error": str(exc)})
                await send_slack_message_idempotent(
                    task_id, user_id, f"RMP did not send {attached['name']}, a file Aura attached: {exc}", bot_token)
                continue
            await _record_receipt(key, "slack.file", {"task_id": task_id, "slack_file": slack_file})
            await _file_event(task_id, "reply.file_sent", {"id": attached["id"], "name": attached["name"],
                                                           "size": now["size"], "slack_file": slack_file})
            sent.append(attached["name"])
    return sent


async def _upload(client: httpx.AsyncClient, bot_token: str, channel: str, path: Path, title: str) -> str:
    """Slack's external upload: an upload URL, the bytes, then the share into the channel; the file's id."""
    data = await asyncio.to_thread(path.read_bytes)
    target = await _slack_call(client, bot_token, "files.getUploadURLExternal",
                               {"filename": path.name, "length": str(len(data))})
    try:
        resp = await client.post(target["upload_url"], files={"file": (path.name, data)}, timeout=300.0)
    except httpx.HTTPError as exc:
        raise SlackTransientError(f"uploading {path.name} failed for now: {exc}")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise SlackTransientError(f"uploading {path.name} failed for now: http {resp.status_code}")
    if resp.status_code != 200:
        raise SlackFileError(f"Slack refused the upload of {path.name}: http {resp.status_code}")
    await _slack_call(client, bot_token, "files.completeUploadExternal",
                      {"files": json.dumps([{"id": target["file_id"], "title": title}]), "channel_id": channel})
    return target["file_id"]


async def _slack_call(client: httpx.AsyncClient, bot_token: str, method: str, form: Dict[str, str]) -> Dict[str, Any]:
    """A Slack Web API call: its result, SlackFileError for Slack's own error, SlackTransientError for now."""
    try:
        resp = await client.post(f"{SLACK_API}/{method}", headers={"Authorization": f"Bearer {bot_token}"},
                                 data=form, timeout=30.0)
    except httpx.HTTPError as exc:
        raise SlackTransientError(f"{method} failed for now: {exc}")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise SlackTransientError(f"{method} failed for now: http {resp.status_code}")
    data = resp.json()
    if data.get("ok"):
        return data
    error = str(data.get("error") or "unknown_error")
    if error in TRANSIENT_SLACK_ERRORS:
        raise SlackTransientError(f"{method} failed for now: {error}")
    raise SlackFileError(f"{method}: {error}")


async def _file_event(task_id: str, event_type: str, payload: Dict[str, Any]) -> None:
    from app.db.models import Event

    async with AsyncSessionLocal() as db:
        db.add(Event(correlation_id=task_id, entity_type="task", entity_id=task_id, event_type=event_type,
                     event_payload=payload))
        await db.commit()
