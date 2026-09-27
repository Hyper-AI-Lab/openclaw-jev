"""Tests for LLM usage monitor."""
import json
import time
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


def test_gpt5_nano_jsonl_attributes_openai_not_nvidia_unknown(usage_paths, monkeypatch):
    utc_day = um._utc_day()
    monkeypatch.setattr(um, "_utc_day", lambda ts=None: utc_day)
    entry = {
        "type": "message",
        "id": "oai-1",
        "timestamp": f"{utc_day}T12:00:00Z",
        "message": {
            "role": "assistant",
            "provider": "openai",
            "model": "openai/gpt-5-nano",
            "usage": {"input": 40, "output": 10, "totalTokens": 50},
            "content": [{"type": "text", "text": "hi"}],
        },
        "stopReason": "stop",
    }
    assert um.record_openclaw_jsonl_message(entry) is True
    summary = um.get_summary()
    assert "nvidia:unknown" not in summary["today_by_profile"]
    assert summary["today_by_profile"]["openai:default"]["requests"] == 1
    assert summary["today_by_profile"]["openai:default"]["total_tokens"] == 50


def test_minimax_jsonl_attributes_nvidia(usage_paths, monkeypatch):
    utc_day = um._utc_day()
    monkeypatch.setattr(um, "_utc_day", lambda ts=None: utc_day)
    entry = {
        "type": "message",
        "id": "mm-1",
        "timestamp": f"{utc_day}T12:00:00Z",
        "message": {
            "role": "assistant",
            "provider": "nvidia",
            "model": "nvidia/minimaxai/minimax-m3",
            "usage": {"input": 10, "output": 5, "totalTokens": 15},
            "content": [{"type": "text", "text": "ok"}],
        },
        "stopReason": "stop",
    }
    assert um.record_openclaw_jsonl_message(entry) is True
    summary = um.get_summary()
    assert summary["today_by_profile"]["nvidia:default"]["requests"] == 1
    assert "nvidia:unknown" not in summary["today_by_profile"]


def test_resolve_usage_profile_id_defaults():
    assert um.resolve_usage_profile_id(None, model="openai/gpt-5-nano") == "openai:default"
    assert um.resolve_usage_profile_id(None, provider="openai") == "openai:default"
    assert um.resolve_usage_profile_id(None, model="nvidia/deepseek-ai/deepseek-v4-flash-0731") == "nvidia:default"
    assert um.resolve_usage_profile_id("nvidia:key2") == "nvidia:key2"
    assert um.resolve_usage_profile_id(None) == "unknown"
    assert (
        um.resolve_usage_profile_id("nvidia:unknown", model="openai/gpt-5-nano")
        == "openai:default"
    )
    assert (
        um.resolve_usage_profile_id("nvidia:unknown", model="nvidia/minimaxai/minimax-m3")
        == "nvidia:default"
    )
    assert um.resolve_usage_profile_id("nvidia:unknown") == "unknown"


def test_rewrite_rolling_unknown_with_model_leaves_day_buckets(usage_paths):
    now_ms = int(time.time() * 1000)
    um._mutate_store(
        lambda store: (
            store["days"].update(
                {
                    "2026-09-05": {
                        "profiles": {
                            "nvidia:unknown": {
                                "by_source": {},
                                "totals": um._zero_counts(),
                            }
                        },
                        "totals": um._zero_counts(),
                    }
                }
            ),
            store["rolling_24h"].extend(
                [
                    {
                        "ts_ms": now_ms,
                        "profile_id": "nvidia:unknown",
                        "source": "openclaw_llm",
                        "model": "openai/gpt-5-nano",
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                        "is_rate_limit": False,
                    },
                    {
                        "ts_ms": now_ms,
                        "profile_id": "nvidia:unknown",
                        "source": "openclaw_llm",
                        "model": "",
                        "input_tokens": 3,
                        "output_tokens": 0,
                        "total_tokens": 3,
                        "is_rate_limit": False,
                    },
                ]
            ),
        )
    )
    summary = um.get_summary()
    store = json.loads(usage_paths.read_text())
    rolling_pids = {e["profile_id"] for e in store["rolling_24h"]}
    assert "openai:default" in rolling_pids
    assert "nvidia:unknown" in store["days"]["2026-09-05"]["profiles"]
    assert "nvidia:key2" not in rolling_pids
    assert "2026-09-05" in summary["unattributed_historical_days"]
    assert summary["unattributed_note"]
    assert "openai:default" in summary["rolling_24h_by_profile"]


def test_record_request_openai_model_not_nvidia_unknown(usage_paths):
    um.record_request("", "openclaw_llm", model="openai/gpt-5-nano", total_tokens=9)
    summary = um.get_summary()
    assert "nvidia:unknown" not in summary["today_by_profile"]
    assert summary["today_by_profile"]["openai:default"]["requests"] == 1
