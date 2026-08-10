import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from temporalio import activity

from app.config import (
    SESSIONS_JSON_PATH,
    get_llm_quota_config,
    get_openclaw_hook_token,
    get_openclaw_url,
    get_slack_bot_token,
    load_settings,
    should_send_intermediate_updates,
    should_suspend_slack,
)
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

OPENCLAW_CONFIG_PATH = "/root/.openclaw/openclaw.json"
SETTINGS_PATH = "/root/.openclaw/rmp/settings.json"

# Only these stop reasons indicate a final assistant turn worth evaluating.
TERMINAL_STOP_REASONS = frozenset({"stop", "error", "maxTokens"})
# OpenClaw/Kimi use "toolUse"; older transcripts may say "toolCalls".
NON_TERMINAL_STOP_REASONS = frozenset({"toolCalls", "toolUse"})

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
    """Reject whitespace-only, stub, or missing-eval replies from RMP agent sessions."""
    cleaned = (text or "").strip()
    if len(cleaned) < 10:
        return False
    lower = cleaned.lower()
    if any(p in lower for p in _INTERIM_RMP_PHRASES) and '"task_status"' not in cleaned:
        if len(cleaned) < 120:
            return False
    if re.search(r'"task_status"\s*:', cleaned):
        return True
    if re.search(r'"facts"\s*:', cleaned):
        return True
    if cleaned == "HEARTBEAT_OK" or cleaned.split("\n")[0].strip() == "HEARTBEAT_OK":
        return True
    # Conversational Slack replies routed through RMP should be substantive.
    return len(cleaned) >= 80


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
        with open(SESSIONS_JSON_PATH, "r") as f:
            sessions = json.load(f)

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

        entry = sessions.get(session_key) or {}
        uid = _from_entry(entry)
        if uid:
            return uid

        # Tasks historically stored agent:main:main while Slack lives under slack:channel:*.
        for key, candidate in sessions.items():
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
    if os.path.exists(SESSIONS_JSON_PATH):
        try:
            with open(SESSIONS_JSON_PATH, "r") as f:
                sessions_data = json.load(f)
            sid = (sessions_data.get(session_key) or {}).get("sessionId")
            if sid and sid not in ids:
                ids.append(sid)
        except Exception:
            pass
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
) -> Tuple[str, str]:
    """Scan multiple session JSONL files for a terminal reply."""
    for sid in session_ids:
        jsonl_path = f"/root/.openclaw/agents/main/sessions/{sid}.jsonl"
        if not os.path.exists(jsonl_path):
            continue
        try:
            with open(jsonl_path, "r") as f:
                lines = f.readlines()
            text_content, stop_reason, _ = _poll_jsonl_for_response(
                jsonl_path, start_time, lines
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


def _poll_jsonl_for_response(
    jsonl_path: str, start_time: float, lines: list
) -> Tuple[str, str, Optional[str]]:
    """Return the latest terminal assistant message after start_time."""
    best_text = ""
    best_reason = ""
    best_ts = 0.0
    latest_rate_error: Optional[str] = None

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


async def _dispatch_openclaw_session(
    internal_session_key: str,
    message: str,
    *,
    poll_timeout_sec: int = 600,
    require_terminal: bool = True,
    task_id: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Gate on LLM quota, dispatch to OpenClaw, poll JSONL; retry on rate limits."""
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
    }
    if model:
        hook_payload["model"] = model

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
        )
        try:
            record_request(profile_id, "openclaw_hook")
            start_time = poll_start_time

            # Snapshot before dispatch — new OpenClaw may create sessionId during POST.
            pre_session_id = None
            if os.path.exists(SESSIONS_JSON_PATH):
                try:
                    with open(SESSIONS_JSON_PATH, "r") as f:
                        pre_data = json.load(f)
                    pre_session_id = (pre_data.get(internal_session_key) or {}).get(
                        "sessionId"
                    )
                except Exception:
                    pass

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
                if os.path.exists(SESSIONS_JSON_PATH):
                    try:
                        with open(SESSIONS_JSON_PATH, "r") as f:
                            sessions_data = json.load(f)
                        if internal_session_key in sessions_data:
                            entry = sessions_data[internal_session_key] or {}
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
                    except Exception:
                        pass
                await asyncio.sleep(1)

            if not session_id:
                raise OpenClawError("Could not find session ID for internal execution.")

            # Pin NVIDIA profile after the session exists (safe on OpenClaw 2026.7+).
            assign_openclaw_session_profile(internal_session_key, profile_id)

            jsonl_path = f"/root/.openclaw/agents/main/sessions/{session_id}.jsonl"
            seen_session_ids: List[str] = []
            text_content = ""
            poll_deadline = time.time() + poll_timeout_sec
            saw_rate_limit = False
            last_jsonl_activity = time.time()
            stall_after_sec = 45

            while time.time() < poll_deadline:
                _safe_activity_heartbeat()
                await _maybe_touch_liveness()
                if session_id and session_id not in seen_session_ids:
                    seen_session_ids.append(session_id)
                if os.path.exists(SESSIONS_JSON_PATH):
                    try:
                        with open(SESSIONS_JSON_PATH, "r") as f:
                            sessions_data = json.load(f)
                        latest = (sessions_data.get(internal_session_key) or {}).get(
                            "sessionId"
                        )
                        if latest and latest != session_id:
                            session_id = latest
                            jsonl_path = (
                                f"/root/.openclaw/agents/main/sessions/{session_id}.jsonl"
                            )
                            if session_id not in seen_session_ids:
                                seen_session_ids.append(session_id)
                    except Exception:
                        pass
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
                    )
                    if fb_text:
                        record_success(profile_id)
                        fb_path = (
                            f"/root/.openclaw/agents/main/sessions/{fallback_ids[0]}.jsonl"
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
                if os.path.exists(jsonl_path):
                    try:
                        with open(jsonl_path, "r") as f:
                            lines = f.readlines()
                        text_content, stop_reason, rate_err = _poll_jsonl_for_response(
                            jsonl_path, start_time, lines
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


@traced_activity("openclaw.dispatch")
async def send_to_openclaw(payload: Dict[str, Any]) -> Dict[str, Any]:
    task_id = payload.get("task_id", "unknown")
    internal_session_key = f"agent:main:rmp_task_{task_id}"
    message = payload.get("message", "") + "\n\n[INTERNAL_RMP]"

    text_content = await _dispatch_openclaw_session(
        internal_session_key,
        message,
        poll_timeout_sec=600,
        require_terminal=True,
        task_id=task_id if task_id != "unknown" else None,
    )
    return {"result": {"payloads": [{"text": text_content}]}}


@traced_activity("slack.notify")
async def notify_slack_user(payload: Dict[str, Any]) -> bool:
    from app.activities.side_effects import send_slack_message_idempotent
    from app.db.database import AsyncSessionLocal
    from app.db.models import Task

    if should_suspend_slack():
        activity.logger.info("Slack delivery suppressed (development_mode)")
        return False

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
        return False

    if not should_deliver_slack(intent, task_type, tags, clean):
        activity.logger.info(
            "Slack delivery suppressed (internal/system): task=%s", task_id
        )
        return False

    bot_token = get_slack_bot_token()
    user_id = _get_slack_user_id(session_key)

    if not bot_token or not user_id:
        activity.logger.warning(
            "Slack delivery skipped: token=%s user=%s", bool(bot_token), bool(user_id)
        )
        return False

    return await send_slack_message_idempotent(
        task_id=task_id,
        user_id=user_id,
        message=clean,
        bot_token=bot_token,
    )


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


@traced_activity("openclaw.verify_quality")
async def verify_response_quality(payload: Dict[str, Any]) -> Dict[str, str]:
    task_id = payload.get("task_id", "unknown")
    user_intent = payload.get("user_intent", "")
    agent_response = payload.get("agent_response", "")

    verification_prompt = f"""You are a QUALITY REVIEWER. Critically evaluate whether the response CORRECTLY and COMPLETELY answers the user's original question.

ORIGINAL USER QUESTION:
{user_intent}

AGENT'S RESPONSE:
{agent_response}

REVIEW CHECKLIST:
1. Does the response actually answer what was asked?
2. Are names, URLs, and references accurate?
3. Is the information complete?
4. Are there factual errors or entity confusion (e.g. Moltbook vs MoltMarket)?

Mark FAIL only for MATERIAL problems. If substantially correct, return PASS.

Respond with ONLY JSON:
{{"quality": "pass", "reason": "brief explanation"}}
or
{{"quality": "fail", "issues": "specific description"}}

[INTERNAL_RMP]"""

    response = await _execute_on_internal_session(task_id, verification_prompt)

    for pattern in [r"(\{[^{}]*\"quality\"[^{}]*\})"]:
        match = re.search(pattern, response, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
                quality = parsed.get("quality", "pass")
                if quality == "fail":
                    return {
                        "quality": "fail",
                        "issues": parsed.get("issues", "Quality check found issues."),
                    }
                return {"quality": "pass", "reason": parsed.get("reason", "")}
            except json.JSONDecodeError:
                pass

    if "fail" in response.lower() and any(
        w in response.lower() for w in ("issue", "incorrect", "wrong")
    ):
        return {"quality": "fail", "issues": "Verification flagged potential issues."}

    return {"quality": "pass", "reason": "No issues detected."}


async def _execute_on_internal_session(task_id: str, message: str) -> str:
    internal_session_key = f"agent:main:rmp_verify_{task_id}"
    try:
        return await _dispatch_openclaw_session(
            internal_session_key,
            message,
            poll_timeout_sec=180,
            require_terminal=False,
        )
    except OpenClawError as e:
        return f"Error: {str(e)}"


async def _execute_intake_llm(intake_id: str, prompt: str) -> str:
    """Fast intake LLM turn on a dedicated internal session."""
    from app.config import get_intake_models, get_intake_timeout_budget

    budget = get_intake_timeout_budget()
    models = get_intake_models()
    if not models:
        models = [None]
    last = "Error: intake LLM failed"
    for idx, model in enumerate(models):
        # Distinct session keys avoid sticky session modelOverride across retries.
        suffix = "" if idx == 0 else f"_fb{idx}"
        internal_session_key = f"agent:main:rmp_intake_{intake_id}{suffix}"
        try:
            text = await _dispatch_openclaw_session(
                internal_session_key,
                prompt,
                poll_timeout_sec=budget["openclaw_poll_sec"],
                require_terminal=False,
                model=model,
            )
        except OpenClawError as e:
            last = f"Error: {str(e)}"
            activity.logger.warning(
                "intake LLM model %s failed: %s", model or "default", last
            )
            continue
        if text and not str(text).strip().lower().startswith("error:"):
            return text
        last = text or last
        activity.logger.warning(
            "intake LLM model %s returned unusable reply; trying next",
            model or "default",
        )
    return last
