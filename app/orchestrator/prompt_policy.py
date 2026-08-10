"""Execution prompt templates — tool budget, English-only replies, task_status JSON."""
from __future__ import annotations

from typing import Optional

GENERIC_PROFILES = {
    "memory_first_read": {
        "patterns": (
            r"\bread\b.*\b(file|document|architecture|md)\b",
            r"\b(architecture\.md|ARCHITECTURE\.md)\b",
            r"\bopenclaw/rmp\b",
        ),
        "tool_budget": 2,
        "memory_hint": (
            "Prefer PROCESS-SCOPED MEMORY and targeted file reads over broad workspace search."
        ),
    },
    "summarize": {
        "patterns": (
            r"\bsummari[sz]e\b",
            r"\btldr\b",
            r"\bbrief overview\b",
        ),
        "tool_budget": 2,
        "memory_hint": "Use process memory for prior context before re-reading large files.",
    },
    "recall": {
        "patterns": (
            r"\bwhat do you remember\b",
            r"\bdo you remember\b",
            r"\brecall\b",
            r"\bwhat.*remember\b",
        ),
        "tool_budget": 1,
        "memory_hint": "Answer from PROCESS-SCOPED MEMORY only.",
    },
    "status": {
        "patterns": (
            r"\btask status\b",
            r"\bwhat('s| is) the status\b",
            r"\bprogress on\b",
        ),
        "tool_budget": 1,
        "memory_hint": "Report status from process memory and step context only.",
    },
    "monitor": {
        "patterns": (
            r"\bmonitor\b",
            r"\bwatch for\b",
            r"\bkeep an eye\b",
        ),
        "tool_budget": 2,
        "memory_hint": "Use process memory for prior monitor state before new checks.",
    },
}

MEMORY_FIRST_UNIVERSAL = (
    "CRITICAL: Use PROCESS-SCOPED MEMORY when present. "
    "Do NOT use memory_search, memory_get, or read workspace MEMORY.md during this RMP step."
)

USER_TIMEZONE = "Asia/Tokyo"
USER_TIMEZONE_LABEL = "Japan Standard Time (JST, UTC+9)"


def user_local_time_block() -> str:
    """Deterministic user-local clock for greetings (server runs Europe/Berlin)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(USER_TIMEZONE))
    hour = now.hour
    if 5 <= hour < 12:
        period = "morning"
    elif 12 <= hour < 17:
        period = "afternoon"
    elif 17 <= hour < 21:
        period = "evening"
    else:
        period = "night"
    return (
        f"USER LOCAL TIME: {now.strftime('%A %Y-%m-%d %H:%M')} {USER_TIMEZONE_LABEL} "
        f"(appropriate greeting period: {period}). "
        f"Kirill lives in Japan — never use Europe/VPS server time for greetings."
    )


def resolve_generic_profile(intent: str) -> Optional[str]:
    import re

    text = intent or ""
    for name, cfg in GENERIC_PROFILES.items():
        for pattern in cfg["patterns"]:
            if re.search(pattern, text, re.IGNORECASE):
                return name
    return None


def profile_tool_budget(profile: Optional[str]) -> int:
    if profile and profile in GENERIC_PROFILES:
        return int(GENERIC_PROFILES[profile]["tool_budget"])
    return 2


def profile_memory_hint(profile: Optional[str]) -> str:
    if profile and profile in GENERIC_PROFILES:
        return GENERIC_PROFILES[profile].get("memory_hint", "")
    return ""


def build_generic_execute_prompt(
    *,
    user_intent: str,
    memory_block: str,
    context_block: str,
    profile: Optional[str] = None,
    user_time_block: Optional[str] = None,
) -> str:
    budget = profile_tool_budget(profile)
    hint = profile_memory_hint(profile)
    extra = f"\nProfile hint: {hint}\n" if hint else ""
    mem = memory_block or "PROCESS-SCOPED MEMORY: (none yet — rely on step instructions only)\n"
    if user_time_block is None:
        tz = user_local_time_block()
    elif user_time_block.strip():
        tz = user_time_block.strip()
    else:
        tz = (
            f"USER LOCAL TIME: Kirill is in {USER_TIMEZONE_LABEL}. "
            "Use Japan-local greetings, not Europe/Berlin VPS server time."
        )
    return f"""User Request: {user_intent}
{tz}
{mem}
{context_block}
{extra}
{MEMORY_FIRST_UNIVERSAL}
Instructions:
1. Execute the required steps. Use tools sparingly (at most {budget} tool calls for simple read/summarize requests).
2. Use the OpenClaw tool named `read` (with `file_path`) to read files — there is no `read_file` tool.
3. After gathering what you need, reply in clear English to Kirill — concise, no mixed languages, no internal planning monologue, no numbered option menus unless the user asked for choices.
4. Do NOT include Origin/Session/Goal/Memory status metadata in your reply.
5. If the user asks to stop, set "stopped": true in facts JSON.
6. Put facts JSON ONLY in a final fenced block (never inline in the user-visible answer):
```json
{{"facts": {{"step_complete": true, "stopped": false}}}}
```
"""


def build_catalog_step_prompt(
    *,
    user_intent: str,
    memory_block: str,
    context_block: str,
    step_prompt: str,
) -> str:
    mem = memory_block or "PROCESS-SCOPED MEMORY: (none yet)\n"
    return f"""User Request: {user_intent}

{mem}
{context_block}

{step_prompt}

{MEMORY_FIRST_UNIVERSAL}
Instructions:
1. Complete ONLY this step. Use tools as needed (prefer at most 3 tool calls unless the step requires more). Use `read` with `file_path` for files (not `read_file`).
2. Reply in clear English — concise, no internal planning monologue or metadata blocks.
3. Put facts JSON ONLY in a final fenced block:
```json
{{"facts": {{"step_complete": true, "blocked": false}}}}
```
Use blocked=true in facts for CAPTCHA/2FA/human approval.
"""
