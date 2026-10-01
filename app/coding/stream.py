"""What a Claude Code run did and how it ended, read from its stream-json output as the run goes.

``claude -p --output-format stream-json --verbose`` writes one JSON event per line. The runner feeds
each complete line to ``feed``, which also keeps progress milestones for notices, and ``outcome``
classifies the run from the final state and the unit's systemd exit line. Unknown events and fields
are ignored; a line that is not a well-formed event is counted in ``malformed``, never raised. An API
failure such as a rejected token or an unknown model ends with subtype "success" and ``is_error`` true.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

MAX_MILESTONES = 50
_COMMAND_CHARS = 120
_ERROR_CHARS = 500
_REPORT_TOOL = "StructuredOutput"
_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_AUTH_ERRORS = {"authentication_failed", "oauth_org_not_allowed", "account_on_hold", "billing_error"}
_RATE_LIMIT_NOTES = {"allowed_warning": "warning", "rejected": "rejected"}
_RETRY_FIELDS = ("attempt", "max_retries", "retry_delay_ms", "error_status", "error")
_DENIAL_FIELDS = ("tool_name", "tool_use_id", "message")
# (summary key, field in the result's usage, field in its modelUsage)
_TOKEN_FIELDS = (
    ("input_tokens", "input_tokens", "inputTokens"),
    ("output_tokens", "output_tokens", "outputTokens"),
    ("cache_read_tokens", "cache_read_input_tokens", "cacheReadInputTokens"),
    ("cache_creation_tokens", "cache_creation_input_tokens", "cacheCreationInputTokens"),
)


@dataclass
class StreamState:
    session_id: Optional[str] = None
    model: Optional[str] = None
    claude_code_version: Optional[str] = None
    permission_mode: Optional[str] = None
    tools: List[str] = field(default_factory=list)
    commands: List[str] = field(default_factory=list)
    # Only the edit tools' files: an edit made through Bash (sed -i) shows in commands alone.
    files_edited: List[str] = field(default_factory=list)
    files_read: List[str] = field(default_factory=list)
    tool_counts: Dict[str, int] = field(default_factory=dict)
    tool_errors: int = 0
    assistant_errors: List[str] = field(default_factory=list)
    api_retries: List[dict] = field(default_factory=list)
    rate_limit: Optional[dict] = None
    permission_denials: List[dict] = field(default_factory=list)
    compactions: int = 0
    background_tasks: Dict[str, str] = field(default_factory=dict)
    result: Optional[dict] = None
    last_text: str = ""
    milestones: List[str] = field(default_factory=list)
    events: int = 0
    malformed: int = 0


@dataclass
class Outcome:
    kind: str
    session_id: Optional[str] = None
    report: Optional[dict] = None
    num_turns: Optional[int] = None
    cost_usd: Optional[float] = None
    usage: dict = field(default_factory=dict)
    error: Optional[str] = None
    resets_at: Optional[int] = None
    exit: Optional[str] = None


def feed(state: StreamState, line: str) -> None:
    """Apply one line of the stream; blank lines are skipped and malformed ones counted."""
    if not line.strip():
        return
    try:
        _apply(state, json.loads(line))
    except Exception:
        state.malformed += 1
    else:
        state.events += 1


def parse_lines(lines: Iterable[str]) -> StreamState:
    """The state after all ``lines``.

    Split the stream on "\\n" alone: ``str.splitlines`` also splits at U+2028, which JSON strings may carry raw.
    """
    state = StreamState()
    for line in lines:
        feed(state, line)
    return state


def outcome(state: StreamState, *, exit_line: Optional[str] = None, stopped: bool = False) -> Outcome:
    """How the run ended, given the unit's ``$SERVICE_RESULT $EXIT_CODE $EXIT_STATUS`` line once it exited.

    ``stopped`` says RMP stopped the run: Claude Code exits 0 after SIGINT and its stream has no result.
    """
    exit_line = exit_line.strip() if exit_line is not None else None
    kind = _kind(state, exit_line, stopped)
    result = state.result or {}
    return Outcome(
        kind=kind,
        session_id=state.session_id,
        report=result.get("structured_output"),
        num_turns=result.get("num_turns"),
        cost_usd=result.get("total_cost_usd"),
        usage=usage_summary(result) if result else {},
        error=None if kind in ("success", "running", "stopped") else _error_text(state),
        resets_at=(state.rate_limit or {}).get("resets_at") if kind == "usage_limit" else None,
        exit=exit_line,
    )


def usage_summary(result: Optional[dict]) -> dict:
    """Tokens and cost of a result event, in total and per model; missing fields count as 0.

    On a resumed session ``cost_usd`` and ``models`` include the earlier runs, the token totals only this run.
    """
    result = result or {}
    usage = result.get("usage") or {}
    summary = {key: usage.get(name) or 0 for key, name, _ in _TOKEN_FIELDS}
    summary["total_tokens"] = sum(summary.values())
    summary["cost_usd"] = result.get("total_cost_usd") or 0.0
    summary["models"] = {
        model: {**{key: counts.get(name) or 0 for key, _, name in _TOKEN_FIELDS},
                "cost_usd": counts.get("costUSD") or 0.0}
        for model, counts in (result.get("modelUsage") or {}).items()
    }
    return summary


def _kind(state: StreamState, exit_line: Optional[str], stopped: bool) -> str:
    result = state.result
    if stopped:
        return "stopped"
    if (exit_line or "").startswith("timeout"):
        return "timeout"
    if result is None:
        return "running" if exit_line is None else "no_result"
    if result.get("subtype") == "error_max_turns":
        return "max_turns"
    if not result.get("is_error") and not (result.get("subtype") or "").startswith("error"):
        return "success"
    status = result.get("api_error_status")
    errors = set(state.assistant_errors) | {retry.get("error") for retry in state.api_retries}
    if (state.rate_limit or {}).get("status") == "rejected" or status == 429 or "rate_limit" in errors:
        return "usage_limit"
    if status in (401, 403) or errors & _AUTH_ERRORS:
        return "auth_failed"
    if result.get("terminal_reason") == "api_error":
        return "api_error"
    return "error"


def _error_text(state: StreamState) -> Optional[str]:
    result = state.result or {}
    text = result.get("result") or "; ".join(result.get("errors") or []) or state.last_text
    return text.strip()[:_ERROR_CHARS] or None


def _apply(state: StreamState, event: dict) -> None:
    kind = event.get("type")
    state.session_id = state.session_id or event.get("session_id")
    if kind == "system":
        _system(state, event)
    elif kind == "assistant":
        if event.get("error"):
            state.assistant_errors.append(event["error"])
        for block in _blocks(event):
            if block.get("type") == "tool_use":
                _tool_use(state, block.get("name"), block.get("input") or {})
            elif block.get("type") == "text" and (block.get("text") or "").strip():
                state.last_text = block["text"].strip()
    elif kind == "user":
        state.tool_errors += sum(1 for block in _blocks(event)
                                 if block.get("type") == "tool_result" and block.get("is_error"))
    elif kind == "rate_limit_event":
        _rate_limit(state, event.get("rate_limit_info") or {})
    elif kind == "result":
        state.result = event


def _system(state: StreamState, event: dict) -> None:
    subtype = event.get("subtype")
    if subtype == "init":
        state.model = event.get("model")
        state.claude_code_version = event.get("claude_code_version")
        state.permission_mode = event.get("permissionMode")
        state.tools = list(event.get("tools") or [])
    elif subtype == "api_retry":
        retry = {key: event.get(key) for key in _RETRY_FIELDS}
        state.api_retries.append(retry)
        _milestone(state, f"API retry {retry['attempt']}/{retry['max_retries']}: {retry['error']}")
    elif subtype == "permission_denied":
        state.permission_denials.append({key: event.get(key) for key in _DENIAL_FIELDS})
    elif subtype == "compact_boundary":
        state.compactions += 1
    elif subtype == "task_started":
        state.background_tasks[event["task_id"]] = "started"
    elif subtype == "task_notification":
        state.background_tasks[event["task_id"]] = event["status"]


def _tool_use(state: StreamState, name: str, args: dict) -> None:
    if name == _REPORT_TOOL:
        _milestone(state, "Structured report ready")
        return
    state.tool_counts[name] = state.tool_counts.get(name, 0) + 1
    path = args.get("file_path") or args.get("notebook_path")
    if name == "Bash" and args.get("command"):
        state.commands.append(args["command"])
        _milestone(state, "Ran: " + (args["command"].strip().splitlines() or [""])[0][:_COMMAND_CHARS])
    elif name in _EDIT_TOOLS and path:
        _add_new(state.files_edited, path)
        _milestone(state, f"Edited {Path(path).name}")
    elif name == "Read" and path:
        _add_new(state.files_read, path)
        _milestone(state, f"Read {Path(path).name}")


def _rate_limit(state: StreamState, info: dict) -> None:
    status, resets_at = info.get("status"), _epoch(info.get("resetsAt"))
    note = _RATE_LIMIT_NOTES.get(status)
    if note and status != (state.rate_limit or {}).get("status"):
        when = f", resets {datetime.fromtimestamp(resets_at, timezone.utc):%Y-%m-%d %H:%M} UTC" if resets_at else ""
        _milestone(state, f"Rate limit {note}{when}")
    state.rate_limit = {
        "status": status,
        "resets_at": resets_at,
        "type": info.get("rateLimitType"),
        "utilization": info.get("utilization"),
        "windows": {name: {"utilization": window.get("utilization"), "resets_at": _epoch(window.get("resetsAt"))}
                    for name, window in (info.get("unifiedWindows") or {}).items()},
    }


def _epoch(value) -> Optional[int]:
    """Epoch seconds from a timestamp in seconds or, above 10**12, milliseconds."""
    if value is None:
        return None
    return int(value // 1000 if value > 10**12 else value)


def _blocks(event: dict) -> list:
    content = (event.get("message") or {}).get("content")
    return content if isinstance(content, list) else []


def _milestone(state: StreamState, text: str) -> None:
    state.milestones.append(text)
    del state.milestones[:-MAX_MILESTONES]


def _add_new(items: List[str], item: str) -> None:
    if item not in items:
        items.append(item)
