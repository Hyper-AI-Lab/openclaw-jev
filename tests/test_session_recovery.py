"""Tests for orphaned OpenClaw session reply recovery."""
from app.orchestrator.session_recovery import (
    _is_terminal_reply,
    extract_user_facing_reply,
)


def test_terminal_reply_with_facts():
    text = 'Hello Kirill\n\n```json\n{"facts": {"step_complete": true}}\n```'
    assert _is_terminal_reply(text) is True


def test_reject_failed_stub():
    assert _is_terminal_reply("[assistant turn failed before producing content]") is False
    assert _is_terminal_reply("short") is False


def test_extract_user_facing_strips_facts():
    raw = (
        "Best tools: grep.app and Hound.\n\n"
        '```json\n{"facts": {"step_complete": true, "stopped": false}}\n```'
    )
    out = extract_user_facing_reply(raw)
    assert "grep.app" in out
    assert "facts" not in out
