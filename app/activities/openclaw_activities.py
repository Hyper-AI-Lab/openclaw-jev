import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
from temporalio import activity

from app.config import (
    OPENCLAW_CONFIG_PATH,
    OPENCLAW_HOME,
    SESSIONS_JSON_PATH,
    SETTINGS_PATH,
    get_llm_quota_config,
    get_openclaw_hook_token,
    get_openclaw_url,
    get_slack_bot_token,
    load_settings,
    should_send_intermediate_updates,
    should_suspend_slack,
)
from app.openclaw_sessions import get_session_entry, iter_session_entries, read_transcript_lines
from app.llm.quota_broker import (
    assign_openclaw_session_profile,
    is_rate_limit_message,
    parse_retry_after,
    record_rate_limit,
    record_success,
    release_profile,
    reserve_profile,
)
from app.llm.usage_monitor import record_jsonl_usage_since, record_request
from app.notification_policy import (
    sanitize_user_facing_text,
    should_deliver_slack,
)
from app.telemetry import traced_activity

# Only these stop reasons indicate a final assistant turn worth evaluating.
TERMINAL_STOP_REASONS = frozenset({"stop", "error", "maxTokens"})
# The fresh sessions a task may use besides its first: rework n, and the recall refinement.
SESSION_SUFFIX = re.compile(r"__(r\d{1,3}|recall)")
# notify_slack_user outcomes: sent; not sent by policy or config; refused by Slack for good.
SLACK_DELIVERED, SLACK_SUPPRESSED, SLACK_REFUSED = "delivered", "suppressed", "refused"
# OpenClaw/Kimi use "toolUse"; older transcripts may say "toolCalls".
NON_TERMINAL_STOP_REASONS = frozenset({"toolCalls", "toolUse"})
# Left of an activity's start-to-close budget for parsing and persisting after the LLM turn.
ACTIVITY_WRAP_UP_SEC = 5.0

# Interim phrases Kimi sometimes emits before tool calls; not a finished RMP turn.
_INTERIM_RMP_PHRASES = (
    "let me check",
    "let me look",
    "give me a moment",
    "one moment",
    "checking my memory",
)


def _safe_activity_heartbeat() -> None:
    """No-op when OpenClaw dispatch runs outside a Temporal activity (API fallback)."""
    try:
        activity.heartbeat()
    except Exception:
        pass


def _is_rmp_terminal_response(text: str) -> bool:
    """Reject empty and interim replies from RMP agent sessions; a short final reply is the answer."""
    cleaned = (text or "").strip()
    first_line = cleaned.split("\n")[0].strip() if cleaned else ""
    if first_line in {"HEARTBEAT_OK", "CANARY_OK"} or first_line.startswith(
        ("HEARTBEAT_OK", "CANARY_OK")
    ):
        return True
    if not cleaned:
        return False
    lower = cleaned.lower()
    if any(p in lower for p in _INTERIM_RMP_PHRASES) and '"task_status"' not in cleaned:
        if len(cleaned) < 120:
            return False
    if re.search(r'"task_status"\s*:', cleaned):
        return True
    if re.search(r'"facts"\s*:', cleaned):
        return True
    # RMP still owns delivery; greetings may be a short paragraph.
    return True


class OpenClawError(Exception):
    pass


def _extract_slack_user_id(value: str) -> str:
    """Pull a Slack user id (U…) from OpenClaw 2026.7 origin fields.

    Live origins look like ``slack:channel:U0AELFYTLKS`` (not ``slack:U…``).
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    match = re.search(r"(U[A-Z0-9]+)", raw)
    return match.group(1) if match else ""


def _get_slack_user_id(session_key: str) -> str:
    from app.config import get_slack_owner_user_id

    try:
        def _from_entry(entry: dict) -> str:
            origin = (entry or {}).get("origin") or {}
            for field in (
                origin.get("from"),
                origin.get("to"),
                origin.get("label"),
                origin.get("id"),
                (entry or {}).get("groupId"),
            ):
                uid = _extract_slack_user_id(str(field or ""))
                if uid:
                    return uid
            return ""

        uid = _from_entry(get_session_entry(session_key, SESSIONS_JSON_PATH))
        if uid:
            return uid

        # Tasks historically stored agent:main:main while Slack lives under slack:channel:*.
        for key, candidate in iter_session_entries(SESSIONS_JSON_PATH):
            if "slack" not in str(key).lower():
                continue
            uid = _from_entry(candidate or {})
            if uid:
                return uid
    except Exception:
        pass
    return get_slack_owner_user_id()


def _parse_msg_timestamp(entry: dict) -> float:
    msg_ts = entry.get("timestamp", entry.get("message", {}).get("timestamp", 0))
    if isinstance(msg_ts, str):
        try:
            dt = datetime.fromisoformat(msg_ts.replace("Z", "+00:00"))
            return dt.timestamp() * 1000
        except Exception:
            return 0
    return float(msg_ts or 0)


def _jsonl_has_recent_activity(lines: list, start_time: float) -> bool:
    for line in lines:
        try:
            entry = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "message":
            continue
        role = entry.get("message", {}).get("role")
        if role not in ("assistant", "toolResult"):
            continue
        if _parse_msg_timestamp(entry) > start_time:
            return True
    return False


def _jsonl_hard_failure(lines: list, start_time: float) -> Optional[str]:
    """Return the All-models-failed error if the agent already exhausted fallbacks."""
    last = None
    for line in lines:
        try:
            entry = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "message":
            continue
        if entry.get("message", {}).get("role") != "assistant":
            continue
        msg_ts = _parse_msg_timestamp(entry)
        if msg_ts <= start_time:
            continue
        stop_reason = entry.get(
            "stopReason", entry.get("message", {}).get("stopReason", "")
        )
        if stop_reason != "error":
            continue
        err_msg = str(entry.get("message", {}).get("errorMessage") or "")
        if is_rate_limit_message(err_msg):
            continue
        blob = (err_msg or _extract_assistant_text(entry) or "").strip()
        if "all models failed" in blob.lower():
            last = blob
    return last


def _reply_deadline(poll_deadline: float, task_id: Optional[str], deadline: Optional[float]) -> float:
    """Aura's reply deadline, moved on while one of her Claude turns in the task works; a hard ``deadline`` stays."""
    from app.coding.direct import task_turn_running

    if task_id and deadline is None and task_turn_running(task_id):
        return max(poll_deadline, time.time() + 120)
    return poll_deadline


def _jsonl_agent_stalled(lines: list, start_time: float) -> bool:
    """True when the latest post-dispatch turn ended without a usable reply."""
    last_assistant: Optional[dict] = None
    last_assistant_ts = 0.0
    for line in lines:
        try:
            entry = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "message":
            continue
        if entry.get("message", {}).get("role") != "assistant":
            continue
        msg_ts = _parse_msg_timestamp(entry)
        if msg_ts <= start_time:
            continue
        if msg_ts >= last_assistant_ts:
            last_assistant = entry
            last_assistant_ts = msg_ts
    if not last_assistant:
        return False
    stop_reason = last_assistant.get(
        "stopReason", last_assistant.get("message", {}).get("stopReason", "")
    )
    if stop_reason in NON_TERMINAL_STOP_REASONS:
        return False
    text = _extract_assistant_text(last_assistant)
    return not _is_rmp_terminal_response(text)


def _recent_session_ids(
    session_key: str,
    pre_session_id: Optional[str],
    current_session_id: Optional[str],
    limit: int = 3,
) -> List[str]:
    """Collect recent session IDs for poll fallback (session-id rotation)."""
    ids: List[str] = []
    for sid in (current_session_id, pre_session_id):
        if sid and sid not in ids:
            ids.append(sid)
    sid = get_session_entry(session_key, SESSIONS_JSON_PATH).get("sessionId")
    if sid and sid not in ids:
        ids.append(sid)
    session_dir = os.path.dirname(SESSIONS_JSON_PATH)
    if os.path.isdir(session_dir):
        files = sorted(
            [f for f in os.listdir(session_dir) if f.endswith(".jsonl")],
            key=lambda n: os.path.getmtime(os.path.join(session_dir, n)),
            reverse=True,
        )
        for fname in files:
            sid = fname.removesuffix(".jsonl")
            if sid not in ids:
                ids.append(sid)
            if len(ids) >= limit:
                break
    return ids[:limit]


def _poll_session_ids_for_response(
    session_ids: List[str],
    start_time: float,
    require_terminal: bool,
    marker: Optional[str] = None,
) -> Tuple[str, str]:
    """Scan multiple session JSONL files for a terminal reply."""
    for sid in session_ids:
        jsonl_path = os.path.join(OPENCLAW_HOME, "agents", "main", "sessions", f"{sid}.jsonl")
        lines = read_transcript_lines(sid)
        if not lines:
            continue
        try:
            text_content, stop_reason, _ = _poll_jsonl_for_response(
                jsonl_path, start_time, lines, marker
            )
            if (
                text_content
                and stop_reason in TERMINAL_STOP_REASONS
                and (not require_terminal or _is_rmp_terminal_response(text_content))
            ):
                return text_content, stop_reason
        except Exception:
            continue
    return "", ""


def _extract_assistant_text(entry: dict) -> str:
    contents = entry.get("message", {}).get("content", [])
    full_text = ""
    for part in contents:
        if isinstance(part, dict):
            if part.get("type") == "text":
                full_text += part.get("text", "")
            elif part.get("type") == "call":
                full_text += f"[Tool Call: {part.get('name')}]"
    return full_text.strip()


def _after_dispatched_turn(lines: list, marker: Optional[str]) -> Optional[list]:
    """Lines after the user turn that carries this dispatch's marker; None until it is written.

    Timestamps alone let a reply to the previous turn of the same session count as this one's.
    """
    if not marker:
        return lines
    for index in range(len(lines) - 1, -1, -1):
        if marker not in lines[index]:
            continue
        try:
            entry = json.loads(lines[index].strip())
        except json.JSONDecodeError:
            continue
        if entry.get("message", {}).get("role") == "user":
            return lines[index + 1:]
    return None


def _poll_jsonl_for_response(
    jsonl_path: str, start_time: float, lines: list, marker: Optional[str] = None
) -> Tuple[str, str, Optional[str]]:
    """Return the latest terminal assistant message after start_time (and after the marker's turn)."""
    best_text = ""
    best_reason = ""
    best_ts = 0.0
    latest_rate_error: Optional[str] = None

    lines = _after_dispatched_turn(lines, marker)
    if lines is None:
        return best_text, best_reason, latest_rate_error
    for line in lines:
        try:
            entry = json.loads(line.strip())
            if entry.get("type") != "message":
                continue
            if entry.get("message", {}).get("role") != "assistant":
                continue

            msg_ts = _parse_msg_timestamp(entry)
            if msg_ts <= start_time:
                continue

            stop_reason = entry.get(
                "stopReason", entry.get("message", {}).get("stopReason", "")
            )
            err_msg = entry.get("message", {}).get("errorMessage", "")
            if stop_reason == "error" and is_rate_limit_message(err_msg):
                latest_rate_error = err_msg
                continue

            text = _extract_assistant_text(entry)
            if not text:
                continue

            # toolUse/toolCalls means the agent is mid-turn — keep polling
            if stop_reason in NON_TERMINAL_STOP_REASONS:
                continue

            if stop_reason in TERMINAL_STOP_REASONS and msg_ts >= best_ts:
                if not _is_rmp_terminal_response(text):
                    continue
                best_text = text
                best_reason = stop_reason
                best_ts = msg_ts
        except json.JSONDecodeError:
            continue

    return best_text, best_reason, latest_rate_error


def _clean_slack_text(message: str) -> str:
    clean = sanitize_user_facing_text(message)
    clean = re.sub(r"\[\[reply_to_current\]\]", "", clean)
    clean = re.sub(r"\[SYSTEM NOTIFICATION\]:?\s*", "", clean)
    clean = re.sub(r"\[SYSTEM ENFORCEMENT\]:?\s*", "", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean


def _activity_deadline(margin_sec: float) -> Optional[float]:
    """Epoch seconds when this activity's start-to-close budget runs out, less ``margin_sec``."""
    try:
        info = activity.info()
    except RuntimeError:
        return None
    if not info.start_to_close_timeout:
        return None
    return (
        info.started_time.timestamp()
        + info.start_to_close_timeout.total_seconds()
        - margin_sec
    )


async def _dispatch_openclaw_session(
    internal_session_key: str,
    message: str,
    *,
    poll_timeout_sec: int = 600,
    require_terminal: bool = True,
    task_id: Optional[str] = None,
    model: Optional[str] = None,
    tags: Optional[List[str]] = None,
    task_type: Optional[str] = None,
    deadline: Optional[float] = None,
    thinking: Optional[str] = None,
) -> str:
    """Gate on LLM quota, dispatch to OpenClaw, poll JSONL; retry on rate limits.

    ``deadline`` (epoch seconds) bounds the quota wait and the reply poll.
    """
    settings = load_settings()
    quota_cfg = get_llm_quota_config()
    headers = {
        "Authorization": f"Bearer {get_openclaw_hook_token()}",
        "Content-Type": "application/json",
    }
    api_endpoint = f"{get_openclaw_url()}/hooks/agent"
    max_dispatch_attempts = 12
    poll_start_time = time.time() * 1000 - 5000
    last_liveness_touch = 0.0
    hook_payload: dict = {
        "sessionKey": internal_session_key,
        "message": message,
        "deliver": False,
        # Trusted RMP-owned sessions — avoid OpenClaw EXTERNAL wrap (NO_REPLY on JSON).
        "allowUnsafeExternalContent": True,
        "sessionMode": "persistent",
    }
    if model:
        hook_payload["model"] = model
    if thinking:
        hook_payload["thinking"] = thinking

    async def _maybe_touch_liveness() -> None:
        nonlocal last_liveness_touch
        if not task_id or task_id == "unknown":
            return
        now = time.time()
        if now - last_liveness_touch < 45:
            return
        last_liveness_touch = now
        try:
            from app.activities.db_activities import touch_task_liveness

            await touch_task_liveness({"task_id": task_id})
        except Exception as exc:
            activity.logger.debug("touch_task_liveness failed: %s", exc)

    for dispatch_attempt in range(max_dispatch_attempts):
        _safe_activity_heartbeat()
        profile_id, slot_id = await reserve_profile(
            session_key=internal_session_key,
            settings=settings,
            heartbeat=activity.heartbeat,
            model=model,
            tags=tags,
            task_type=task_type,
            deadline=deadline,
        )
        try:
            record_request(profile_id, "openclaw_hook")
            start_time = poll_start_time
            marker = f"[RMP_DISPATCH {uuid.uuid4().hex[:12]}]"
            hook_payload["message"] = f"{marker}\n{message}"

            # Snapshot before dispatch — new OpenClaw may create sessionId during POST.
            pre_session_id = get_session_entry(
                internal_session_key, SESSIONS_JSON_PATH
            ).get("sessionId")

            async with httpx.AsyncClient() as client:
                last_err = None
                for attempt in range(10):
                    try:
                        resp = await client.post(
                            api_endpoint,
                            json=hook_payload,
                            headers=headers,
                            timeout=30.0,
                        )
                        if resp.status_code == 429:
                            retry_after = parse_retry_after(
                                dict(resp.headers), resp.text
                            )
                            record_rate_limit(
                                profile_id, retry_after, settings=settings
                            )
                            last_err = OpenClawError("API rate limit reached")
                            break
                        resp.raise_for_status()
                        last_err = None
                        break
                    except httpx.HTTPError as e:
                        last_err = e
                        _safe_activity_heartbeat()
                        await asyncio.sleep(2)
            if last_err:
                if isinstance(last_err, OpenClawError) and "rate limit" in str(
                    last_err
                ).lower():
                    continue
                raise OpenClawError(f"Failed to call OpenClaw: {str(last_err)}")

            session_id = None
            for _ in range(60):
                _safe_activity_heartbeat()
                entry = get_session_entry(internal_session_key, SESSIONS_JSON_PATH)
                if entry:
                    candidate = entry.get("sessionId")
                    if candidate and candidate != pre_session_id:
                        session_id = candidate
                        break
                    if candidate and not pre_session_id:
                        session_id = candidate
                        break
                    # Session key reused with same sessionId (common on OpenClaw 2026.7+).
                    if candidate and (
                        entry.get("status") == "running"
                        or float(entry.get("updatedAt") or 0) >= start_time
                    ):
                        session_id = candidate
                        break
                await asyncio.sleep(1)

            if not session_id:
                raise OpenClawError("Could not find session ID for internal execution.")

            # Pin NVIDIA profile only for nvidia/* models (never openai/*).
            from app.llm.model_policy import should_pin_nvidia_profile

            if should_pin_nvidia_profile(model, profile_id):
                assign_openclaw_session_profile(internal_session_key, profile_id)

            jsonl_path = os.path.join(OPENCLAW_HOME, "agents", "main", "sessions", f"{session_id}.jsonl")
            seen_session_ids: List[str] = []
            text_content = ""
            poll_deadline = time.time() + poll_timeout_sec
            if deadline is not None:
                poll_deadline = min(poll_deadline, deadline)
            saw_rate_limit = False
            last_jsonl_activity = time.time()
            stall_after_sec = 45

            while time.time() < poll_deadline:
                _safe_activity_heartbeat()
                await _maybe_touch_liveness()
                if time.time() > poll_deadline - 60:
                    poll_deadline = await asyncio.to_thread(_reply_deadline, poll_deadline, task_id, deadline)
                if session_id and session_id not in seen_session_ids:
                    seen_session_ids.append(session_id)
                latest = get_session_entry(
                    internal_session_key, SESSIONS_JSON_PATH
                ).get("sessionId")
                if latest and latest != session_id:
                    session_id = latest
                    jsonl_path = (
                        os.path.join(OPENCLAW_HOME, "agents", "main", "sessions", f"{session_id}.jsonl")
                    )
                    if session_id not in seen_session_ids:
                        seen_session_ids.append(session_id)
                if time.time() > poll_deadline - 30 and not text_content:
                    fallback_ids = _recent_session_ids(
                        internal_session_key,
                        pre_session_id,
                        session_id,
                        limit=3,
                    )
                    fb_text, fb_reason = _poll_session_ids_for_response(
                        fallback_ids,
                        start_time,
                        require_terminal,
                        marker,
                    )
                    if fb_text:
                        record_success(profile_id)
                        fb_path = (
                            os.path.join(OPENCLAW_HOME, "agents", "main", "sessions", f"{fallback_ids[0]}.jsonl")
                            if fallback_ids
                            else jsonl_path
                        )
                        record_jsonl_usage_since(
                            fb_path,
                            start_time,
                            profile_id=profile_id,
                            session_key=internal_session_key,
                        )
                        return fb_text
                lines = read_transcript_lines(session_id) if session_id else []
                if lines:
                    try:
                        hard_fail = _jsonl_hard_failure(lines, start_time)
                        if hard_fail:
                            raise OpenClawError(hard_fail)
                        text_content, stop_reason, rate_err = _poll_jsonl_for_response(
                            jsonl_path, start_time, lines, marker
                        )
                        if _jsonl_has_recent_activity(lines, start_time):
                            last_jsonl_activity = time.time()
                        if rate_err:
                            saw_rate_limit = True
                        if (
                            text_content
                            and stop_reason in TERMINAL_STOP_REASONS
                            and (
                                not require_terminal
                                or _is_rmp_terminal_response(text_content)
                            )
                        ):
                            record_success(profile_id)
                            record_jsonl_usage_since(
                                jsonl_path,
                                start_time,
                                profile_id=profile_id,
                                session_key=internal_session_key,
                            )
                            return text_content
                        if saw_rate_limit and not text_content:
                            record_rate_limit(profile_id, settings=settings)
                            break
                        if (
                            time.time() - last_jsonl_activity > stall_after_sec
                            and _jsonl_agent_stalled(lines, start_time)
                        ):
                            raise OpenClawError(
                                "Agent stopped without a usable reply (empty or tool-only turn)."
                            )
                        text_content = ""
                    except OpenClawError:
                        raise
                    except Exception as e:
                        activity.logger.warning(f"Error reading JSONL: {e}")
                await asyncio.sleep(1)

            if saw_rate_limit:
                activity.logger.info(
                    "Rate limit during poll (attempt %s/%s); waiting for next key",
                    dispatch_attempt + 1,
                    max_dispatch_attempts,
                )
                continue

            if not text_content:
                raise OpenClawError("Timed out waiting for agent reply.")
        finally:
            await release_profile(
                session_key=internal_session_key,
                slot_id=slot_id,
            )

    raise OpenClawError(
        "Timed out waiting for agent reply after rate-limit retries."
    )


@traced_activity("openclaw.actions_digest")
async def task_actions_digest(payload: Dict[str, Any]) -> str:
    """What Aura already did in this task, for a rework that starts in a fresh session."""
    from app.openclaw_sessions import task_action_trace
    from app.orchestrator.process_evaluator import format_action_trace

    trace = await asyncio.to_thread(task_action_trace, payload.get("task_id", ""), limit=60)
    return format_action_trace(trace)[:6000]


@traced_activity("openclaw.dispatch")
async def send_to_openclaw(payload: Dict[str, Any]) -> Dict[str, Any]:
    from app.config import get_primary_agent_model
    from app.llm.model_policy import TASK_THINKING, THINKING_DEFAULT
    from app.notification_policy import is_internal_task

    task_id = payload.get("task_id", "unknown")
    # Reworks and the recall refinement run in fresh sessions (__r<n>, __recall), so their
    # context is the brief they carry, not every earlier attempt.
    suffix = str(payload.get("session_suffix") or "")
    if suffix and not SESSION_SUFFIX.fullmatch(suffix):
        raise ValueError(f"invalid session suffix {suffix!r}")
    internal_session_key = f"agent:main:rmp_task_{task_id}{suffix}"
    message = payload.get("message", "") + "\n\n[INTERNAL_RMP]"
    # User-facing / plan execute turns use policy primary unless caller overrides.
    model = payload.get("model") or get_primary_agent_model()
    tags = payload.get("tags") or []
    task_type = payload.get("task_type") or ""
    internal = is_internal_task("", task_type, tags)

    text_content = await _dispatch_openclaw_session(
        internal_session_key,
        message,
        poll_timeout_sec=600,
        require_terminal=True,
        task_id=task_id if task_id != "unknown" else None,
        model=model,
        tags=tags,
        task_type=task_type,
        thinking=THINKING_DEFAULT if internal else TASK_THINKING,
    )
    return {"result": {"payloads": [{"text": text_content}]}}


# Union: histories recorded before Sep 30 2026 hold booleans, and replays decode them through this hint.
@traced_activity("slack.notify")
async def notify_slack_user(payload: Dict[str, Any]) -> Union[bool, str]:
    from app.activities.side_effects import send_slack_message_idempotent
    from app.db.database import AsyncSessionLocal
    from app.db.models import Task

    if should_suspend_slack():
        activity.logger.info("Slack delivery suppressed (development_mode)")
        return SLACK_SUPPRESSED

    session_key = payload.get("session_key", "agent:main:main")
    message = payload.get("message", "")
    task_id = payload.get("task_id", "unknown")
    intent = payload.get("intent", "")
    task_type = payload.get("task_type", "")
    tags = payload.get("tags") or []

    if not intent and task_id not in ("unknown", ""):
        try:
            async with AsyncSessionLocal() as db:
                task = await db.get(Task, task_id)
                if task:
                    intent = task.goal or intent
                    task_type = task_type or task.task_type or ""
        except Exception as e:
            activity.logger.warning("Task lookup for Slack policy failed: %s", e)

    clean = _clean_slack_text(message)
    if not clean:
        return SLACK_SUPPRESSED

    if not should_deliver_slack(intent, task_type, tags, clean):
        activity.logger.info(
            "Slack delivery suppressed (internal/system): task=%s", task_id
        )
        return SLACK_SUPPRESSED

    bot_token = get_slack_bot_token()
    user_id = _get_slack_user_id(session_key)

    if not bot_token or not user_id:
        activity.logger.warning(
            "Slack delivery skipped: token=%s user=%s", bool(bot_token), bool(user_id)
        )
        return SLACK_SUPPRESSED

    delivered = await send_slack_message_idempotent(
        task_id=task_id,
        user_id=user_id,
        message=clean,
        bot_token=bot_token,
        kind=str(payload.get("message_kind") or "notice"),
        session_key=session_key,
        meta={k: payload[k] for k in ("attempt", "process_run_id") if payload.get(k) is not None},
    )
    return SLACK_DELIVERED if delivered else SLACK_REFUSED


@traced_activity("openclaw.validate_output")
async def validate_openclaw_output(payload: Dict[str, Any]) -> Dict[str, Any]:
    text = payload.get("text", "")
    if not text or len(text.strip()) < 3:
        return {"is_valid": False, "error": "Empty or trivial response"}
    if text.strip().startswith("[Tool Call:") and len(text.strip()) < 100:
        return {"is_valid": False, "error": "Tool-call-only response"}
    return {"is_valid": True, "text": text}


@traced_activity("openclaw.parse_evaluation")
async def parse_agent_evaluation(payload: Dict[str, Any]) -> Dict[str, str]:
    from app.orchestrator.decision_engine import (
        decide_step_outcome,
        merge_evaluation_with_decision,
    )
    from app.orchestrator.step_predicates import extract_agent_facts

    text = payload.get("text", "")
    extracted = extract_agent_facts(text)
    legacy_status = extracted.get("legacy_status", "pending")
    legacy_reason = extracted.get("legacy_reason") or "No reason provided"
    facts = extracted.get("facts") or {}

    if facts.get("step_complete") or facts.get("deliverable"):
        result = {"status": "completed", "reason": legacy_reason or "Facts indicate complete"}
    elif legacy_status in (
        "completed",
        "pending",
        "failed",
        "stopped_by_user",
        "blocked",
        "needs_replan",
    ):
        result = {"status": legacy_status, "reason": legacy_reason}
    else:
        text_lower = text.lower()
        if any(
            p in text_lower
            for p in ("stopped by user", "stopped by the user", "stop the task")
        ):
            result = {"status": "stopped_by_user", "reason": "User requested to stop."}
        elif any(
            p in text_lower
            for p in ('"task_status": "completed"', "task is complete", "task finished")
        ):
            result = {"status": "completed", "reason": "Inferred from text"}
        elif "task failed" in text_lower or '"task_status": "failed"' in text_lower:
            result = {"status": "failed", "reason": "Inferred from text"}
        elif '"task_status": "blocked"' in text_lower or "blocked:" in text_lower:
            result = {"status": "blocked", "reason": "Inferred from text"}
        elif "needs_replan" in text_lower or "needs replan" in text_lower:
            result = {"status": "needs_replan", "reason": "Inferred from text"}
        else:
            result = {
                "status": "pending",
                "reason": "Evaluation block not found or invalid.",
            }

    if payload.get("orchestrate"):
        decision = decide_step_outcome(
            parsed_status=result.get("status", "pending"),
            reason=result.get("reason", ""),
            validation_ok=bool(payload.get("validation_ok", True)),
            attempt=int(payload.get("attempt", 1)),
            max_attempts=int(payload.get("max_attempts", 10)),
        )
        result = merge_evaluation_with_decision(result, decision)

    return result


@activity.defn
async def check_intermediate_updates_enabled(payload: Dict[str, Any]) -> bool:
    return should_send_intermediate_updates()


PR_EVENTS = ("coding.pr_merged", "coding.pr_not_merged")


async def _direct_claude_evidence(task_id: str) -> str:
    """Aura's direct Claude sessions in the task and the pull requests RMP merged for her, from RMP's records.

    A reviewed coding job brings its own evidence; this is what Aura did with Claude herself.
    """
    from sqlalchemy import select

    from app.coding.records import section_text, task_records
    from app.db.database import AsyncSessionLocal
    from app.db.models import Event
    from app.memory.policy import redact_secrets

    sessions = [r for r in await asyncio.to_thread(task_records, task_id) if r["kind"] == "session"]
    async with AsyncSessionLocal() as db:
        events = (await db.execute(
            select(Event).where(Event.entity_id == task_id, Event.event_type.in_((*PR_EVENTS, "coding.deploy")))
            .order_by(Event.occurred_at)
        )).scalars().all()
    if not any(e.event_type in PR_EVENTS for e in events):
        events = []
    lines = ["Aura's Claude sessions (what she asked, what Claude did and answered):\n"
             + section_text(sessions, reply_chars=1500)] if sessions else []
    for event in events:
        p = event.event_payload or {}
        if event.event_type == "coding.pr_merged":
            lines.append(f"RMP merged PR #{p.get('pr')} ({p.get('url')}) after CI's test check passed, as {str(p.get('merge'))[:12]}.")
        elif event.event_type == "coding.pr_not_merged":
            lines.append(f"RMP did not merge PR #{p.get('pr')}: {p.get('summary')}")
        else:
            lines.append(f"Deploy {p.get('status')}: {p.get('summary')}")
    return redact_secrets("\n".join(lines))


@traced_activity("openclaw.verify_quality")
async def verify_response_quality(payload: Dict[str, Any]) -> Dict[str, Any]:
    from app.artifacts.store import ArtifactStore
    from app.openclaw_sessions import task_action_trace
    from app.orchestrator.process_evaluator import (
        build_evaluator_prompt,
        format_action_trace,
        format_artifacts,
        format_external_evidence,
        parse_evaluator_response,
        persist_evaluator_verdict,
    )
    from app.orchestrator.evaluator_tools import collect_situational_context

    task_id = payload.get("task_id", "unknown")
    enriched = dict(payload)
    enriched["external_evidence_text"] = format_external_evidence(payload.get("external_evidence"))
    try:
        direct = await _direct_claude_evidence(task_id)
    except Exception as exc:
        activity.logger.warning("Claude evidence unavailable for %s: %s", task_id, exc)
        direct = ""
    enriched["external_evidence_text"] = "\n\n".join(p for p in (enriched["external_evidence_text"], direct) if p)
    try:
        enriched["situational_tools"] = await collect_situational_context(payload)
    except Exception:
        enriched["situational_tools"] = "(situational tools unavailable)"
    try:
        enriched["tools_taken"] = format_action_trace(await asyncio.to_thread(task_action_trace, task_id))
    except Exception as exc:
        activity.logger.warning("Action trace unavailable for %s: %s", task_id, exc)
        enriched["tools_taken"] = "(action trace unavailable)"
    try:
        enriched["artifacts"] = format_artifacts(
            await ArtifactStore.list_for_process(payload.get("process_run_id") or "")
        )
    except Exception as exc:
        activity.logger.warning("Artifact list unavailable for %s: %s", task_id, exc)
        enriched["artifacts"] = "(artifact list unavailable)"
    verification_prompt = build_evaluator_prompt(enriched)
    response = await _execute_on_internal_session(
        task_id, verification_prompt, verdict=int(payload.get("attempt") or 0)
    )
    result = parse_evaluator_response(response)
    await persist_evaluator_verdict(payload, result)
    return result


async def _execute_on_internal_session(task_id: str, message: str, *, verdict: int = 0) -> str:
    """Process Evaluator turn: the primary, then the NVIDIA fallback, each with an explicit model.

    Each verdict gets its own session (``__v<n>``): a judgment never reads the earlier ones.
    """
    from app.llm.model_policy import FALLBACK_MODELS, SUBAGENT_MODEL, drop_unwired_openai
    from app.orchestrator.process_evaluator import parse_evaluator_response

    deadline = _activity_deadline(ACTIVITY_WRAP_UP_SEC)
    models = drop_unwired_openai([SUBAGENT_MODEL, *FALLBACK_MODELS]) or [None]
    last = "Error: evaluator produced no verdict"
    for idx, model in enumerate(models):
        # A separate session per model, as intake does: no sticky model override or
        # half-written failed turn carries over to the fallback.
        suffix = (f"__v{verdict}" if verdict else "") + ("" if idx == 0 else f"_fb{idx}")
        try:
            text = await _dispatch_openclaw_session(
                f"agent:main:rmp_verify_{task_id}{suffix}",
                message,
                poll_timeout_sec=180,
                require_terminal=False,
                task_id=task_id,
                model=model,
                deadline=deadline,
            )
        except (OpenClawError, TimeoutError) as e:
            last = f"Error: {str(e)}"
            activity.logger.warning(
                "evaluator model %s failed: %s", model or "default", last
            )
            continue
        if not parse_evaluator_response(text).get("parse_error"):
            return text
        last = text
        activity.logger.warning(
            "evaluator model %s returned no usable verdict; trying next",
            model or "default",
        )
    return last


async def _execute_intake_llm(intake_id: str, prompt: str) -> str:
    """Fast intake LLM turn on a dedicated internal session."""
    from app.config import get_intake_models, get_intake_timeout_budget

    budget = get_intake_timeout_budget()
    models = get_intake_models()
    if not models:
        models = [None]
    deadline = _activity_deadline(ACTIVITY_WRAP_UP_SEC)
    last = "Error: intake LLM failed"
    for idx, model in enumerate(models):
        # Distinct session keys avoid sticky session modelOverride and poisoned
        # transcripts from a prior failed intake of the same fingerprint.
        nonce = uuid.uuid4().hex[:8]
        suffix = "" if idx == 0 else f"_fb{idx}"
        internal_session_key = f"agent:main:rmp_intake_{intake_id}_{nonce}{suffix}"
        try:
            text = await _dispatch_openclaw_session(
                internal_session_key,
                prompt,
                poll_timeout_sec=budget["openclaw_poll_sec"],
                require_terminal=False,
                model=model,
                deadline=deadline,
            )
        except OpenClawError as e:
            last = f"Error: {str(e)}"
            activity.logger.warning(
                "intake LLM model %s failed: %s", model or "default", last
            )
            continue
        if text and not str(text).strip().lower().startswith("error:"):
            from app.task_registry.intake_prompt import parse_intake_response

            parsed = parse_intake_response(text)
            if parsed.get("rationale") != "Failed to parse intake JSON":
                return text
            last = text
            activity.logger.warning(
                "intake LLM model %s returned unparseable JSON; trying next",
                model or "default",
            )
            continue
        last = text or last
        activity.logger.warning(
            "intake LLM model %s returned unusable reply; trying next",
            model or "default",
        )
    return last
