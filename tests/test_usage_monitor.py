"""Tests for LLM usage monitor."""
import json
from pathlib import Path

import pytest

from app.llm import usage_monitor as um


@pytest.fixture
def usage_paths(monkeypatch, tmp_path):
    usage_file = tmp_path / "llm_usage.json"
    cursor_file = tmp_path / "llm_usage_scrape_cursor.json"
    lock_file = tmp_path / ".llm_usage.lock"
    monkeypatch.setattr(um, "USAGE_PATH", usage_file)
    monkeypatch.setattr(um, "CURSOR_PATH", cursor_file)
    monkeypatch.setattr(um, "LOCK_PATH", lock_file)
    monkeypatch.setattr(um, "scrape_openclaw_sessions", lambda **_: {"new_events": 0})
    return usage_file


def test_record_request_and_summary(usage_paths):
    um.record_request(
        "nvidia:default",
        "embed",
        input_tokens=100,
        output_tokens=0,
        total_tokens=100,
        model="nvidia/nv-embed-v1",
    )
    um.record_request("nvidia:key2", "openclaw_hook")
    summary = um.get_summary()
    assert summary["today_totals"]["requests"] == 2
    assert summary["today_totals"]["total_tokens"] == 100
    assert summary["today_by_profile"]["nvidia:default"]["requests"] == 1


def test_jsonl_message_dedupe(usage_paths, monkeypatch):
    utc_day = um._utc_day()
    monkeypatch.setattr(um, "_utc_day", lambda ts=None: utc_day)
    entry = {
        "type": "message",
        "id": "abc123",
        "timestamp": f"{utc_day}T12:00:00Z",
        "message": {
            "role": "assistant",
            "provider": "nvidia",
            "model": "moonshotai/kimi-k2.6",
            "usage": {"input": 500, "output": 20, "totalTokens": 520},
            "content": [{"type": "text", "text": "ok"}],
        },
        "stopReason": "stop",
    }
    assert um.record_openclaw_jsonl_message(entry, profile_id="nvidia:default") is True
    assert um.record_openclaw_jsonl_message(entry, profile_id="nvidia:default") is False
    summary = um.get_summary()
    assert summary["today_by_profile"]["nvidia:default"]["requests"] == 1
    assert summary["today_by_profile"]["nvidia:default"]["total_tokens"] == 520
