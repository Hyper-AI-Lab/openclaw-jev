import json

from app.activities.openclaw_activities import (
    _is_rmp_terminal_response,
    _jsonl_agent_stalled,
    _poll_jsonl_for_response,
    _poll_session_ids_for_response,
    _recent_session_ids,
)


def _line(entry: dict) -> str:
    return json.dumps(entry) + "\n"


def test_poll_ignores_tool_use_turns():
    lines = [
        _line(
            {
                "type": "message",
                "timestamp": "2026-06-04T06:28:56.002Z",
                "stopReason": "toolUse",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Let me read that file."}],
                },
            }
        ),
        _line(
            {
                "type": "message",
                "timestamp": "2026-06-04T06:29:08.792Z",
                "stopReason": "stop",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": " "}],
                },
            }
        ),
    ]
    text, reason, _ = _poll_jsonl_for_response("x.jsonl", 0, lines)
    assert text == ""
    assert reason == ""


def test_poll_accepts_task_status_reply():
    body = (
        "Here is the summary.\n"
        '{"task_status": "completed", "reason": "Read ARCHITECTURE.md"}'
    )
    lines = [
        _line(
            {
                "type": "message",
                "timestamp": "2026-06-04T06:29:10.000Z",
                "stopReason": "stop",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": body}],
                },
            }
        )
    ]
    text, reason, _ = _poll_jsonl_for_response("x.jsonl", 0, lines)
    assert "summary" in text
    assert reason == "stop"
    assert _is_rmp_terminal_response(text)


def test_agent_stalled_after_empty_stop():
    lines = [
        _line(
            {
                "type": "message",
                "timestamp": "2026-06-04T06:38:59.782Z",
                "stopReason": "toolUse",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "name": "read"}],
                },
            }
        ),
        _line(
            {
                "type": "message",
                "timestamp": "2026-06-04T06:39:02.686Z",
                "stopReason": "stop",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": " "}],
                },
            }
        ),
    ]
    assert _jsonl_agent_stalled(lines, 0) is True


def test_agent_not_stalled_while_tool_use_pending():
    lines = [
        _line(
            {
                "type": "message",
                "timestamp": "2026-06-04T06:38:59.782Z",
                "stopReason": "toolUse",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "name": "read"}],
                },
            }
        )
    ]
    assert _jsonl_agent_stalled(lines, 0) is False


def test_recent_session_ids_includes_current_and_pre():
    ids = _recent_session_ids("agent:main:rmp_task_x", "pre-id", "cur-id", limit=3)
    assert ids[0] == "cur-id"
    assert "pre-id" in ids


def test_poll_session_ids_fallback_finds_reply(monkeypatch):
    from unittest.mock import mock_open

    sid = "abc-123"

    def fake_poll(path, start_time, lines):
        return (
            'Summary here with enough length for terminal check. '
            '{"facts": {"step_complete": true}}',
            "stop",
            None,
        )

    monkeypatch.setattr(
        "app.activities.openclaw_activities._poll_jsonl_for_response",
        fake_poll,
    )
    monkeypatch.setattr(
        "app.activities.openclaw_activities.read_transcript_lines",
        lambda sid: ["{}"],
    )
    monkeypatch.setattr(
        "app.activities.openclaw_activities.os.path.exists",
        lambda p: True,
    )
    monkeypatch.setattr("builtins.open", mock_open(read_data="{}\n"))
    text, reason = _poll_session_ids_for_response([sid], 0, True)
    assert "Summary" in text
    assert reason == "stop"
    assert _is_rmp_terminal_response(text)


def test_poll_accepts_facts_terminal():
    body = 'All done.\n{"facts": {"step_complete": true}}'
    lines = [
        _line(
            {
                "type": "message",
                "timestamp": "2026-06-04T06:29:10.000Z",
                "stopReason": "stop",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": body}],
                },
            }
        )
    ]
    text, reason, _ = _poll_jsonl_for_response("x.jsonl", 0, lines)
    assert _is_rmp_terminal_response(text)


def test_terminal_accepts_short_greeting_and_canary_ok():
    assert _is_rmp_terminal_response("Yes, I'm here.") is True
    assert _is_rmp_terminal_response("CANARY_OK") is True
    assert _is_rmp_terminal_response("OK") is False
    assert _is_rmp_terminal_response("Let me check") is False


def test_hard_failure_detects_all_models_failed():
    from app.activities.openclaw_activities import _jsonl_hard_failure

    lines = [
        _line(
            {
                "type": "message",
                "timestamp": "2026-09-05T12:50:38.000Z",
                "stopReason": "error",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "errorMessage": (
                        "All models failed (3): openai/gpt-5-nano: LLM request timed out."
                    ),
                },
            }
        )
    ]
    err = _jsonl_hard_failure(lines, 0)
    assert err and "All models failed" in err
