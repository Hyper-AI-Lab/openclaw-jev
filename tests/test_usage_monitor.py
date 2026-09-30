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
    monkeypatch.setattr(um, "AGENT_DB_PATH", tmp_path / "no-openclaw-agent.sqlite")
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


NOW_MS = 1_790_600_000_000
HOUR_MS = 3_600_000
CANARY_TASK = "11111111-2222-3333-4444-555555555555"
USER_TASK = "66666666-7777-8888-9999-000000000000"


@pytest.fixture
def agent_db(monkeypatch, tmp_path):
    """A minimal OpenClaw 2026.9 agent store: transcripts, session windows, session nodes."""
    import sqlite3

    path = tmp_path / "openclaw-agent.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, event_json TEXT, created_at INTEGER);
        CREATE TABLE session_windows (session_id TEXT PRIMARY KEY, session_key TEXT);
        CREATE TABLE session_nodes (session_key TEXT PRIMARY KEY, current_session_id TEXT, archived_at INTEGER);
        """
    )
    con.commit()
    monkeypatch.setattr(um, "AGENT_DB_PATH", path)
    monkeypatch.setattr(
        um, "_task_kinds_sync", lambda ids: {CANARY_TASK: "canary", USER_TASK: "user"}
    )
    monkeypatch.setattr(um, "_ended_task_ids_sync", lambda ids: set())

    class Store:
        def __init__(self):
            self.seq = {}

        def session(self, session_id, session_key, *, archived=False):
            con.execute("INSERT INTO session_windows VALUES (?, ?)", (session_id, session_key))
            con.execute(
                "INSERT INTO session_nodes VALUES (?, ?, ?)",
                (session_key, session_id, NOW_MS if archived else None),
            )
            con.commit()

        def turn(self, session_id, at_ms, *, stop="stop", prompt=0, cached=0, output=0,
                 provider="openai", model="gpt-5-nano", msg_id=None):
            seq = self.seq.get(session_id, 0) + 1
            self.seq[session_id] = seq
            event = {
                "type": "message",
                "id": msg_id or f"{session_id}-{seq}",
                "timestamp": at_ms,
                "message": {
                    "role": "assistant",
                    "provider": provider,
                    "model": model,
                    "stopReason": stop,
                    "errorMessage": "LLM idle timeout (5s): no response from model" if stop == "aborted" else None,
                    "usage": {"input": prompt - cached, "cacheRead": cached, "output": output,
                              "totalTokens": prompt + output},
                    "timestamp": at_ms,
                },
            }
            con.execute(
                "INSERT INTO transcript_events VALUES (?, ?, ?, ?)",
                (session_id, seq, json.dumps(event), at_ms),
            )
            con.commit()

    yield Store()
    con.close()


def test_transcript_usage_attributes_categories_and_aborted_prompts(agent_db):
    agent_db.session("hb", "agent:main:heartbeat", archived=True)
    agent_db.session("in", "agent:main:rmp_intake_abc123_def456")
    agent_db.session("ca", f"agent:main:rmp_task_{CANARY_TASK}")
    agent_db.session("tk", f"agent:main:rmp_task_{USER_TASK}")
    agent_db.session("dm", "agent:main:slack:channel:d0test")

    agent_db.turn("hb", NOW_MS - 30 * HOUR_MS, prompt=90_000)  # outside the 24 h window
    agent_db.turn("hb", NOW_MS - 5 * HOUR_MS, prompt=100_000, output=10)
    agent_db.turn("hb", NOW_MS - 4 * HOUR_MS, stop="aborted")
    agent_db.turn("hb", NOW_MS - 4 * HOUR_MS + 6000, stop="aborted")
    agent_db.turn("hb", NOW_MS - 4 * HOUR_MS + 12000, prompt=120_000, cached=20_000, output=5)
    agent_db.turn("in", NOW_MS - HOUR_MS, prompt=5_000, output=300)
    agent_db.turn("ca", NOW_MS - 2 * HOUR_MS, prompt=20_000, output=50)
    agent_db.turn("tk", NOW_MS - 2 * HOUR_MS, prompt=30_000, output=500)
    agent_db.turn("dm", NOW_MS - HOUR_MS, provider="openclaw", model="delivery-mirror")

    report = um.transcript_usage(hours=24, now_ms=NOW_MS)
    cats = report["by_category"]
    assert set(cats) == {"heartbeat", "intake", "canary", "task"}
    hb = cats["heartbeat"]
    assert (hb["attempts"], hb["aborted"]) == (4, 2)
    assert hb["input_tokens"] == 100_000 + 100_000
    assert hb["cache_read_tokens"] == 20_000
    assert hb["aborted_prompt_tokens"] == 2 * 120_000
    assert cats["canary"]["input_tokens"] == 20_000
    assert cats["task"]["input_tokens"] == 30_000
    assert report["totals"]["attempts"] == 7
    assert report["abort_rate"] == pytest.approx(2 / 7)
    # The heartbeat is archived, so the largest live context is the user task.
    assert report["max_live_context"] == {
        "session_key": f"agent:main:rmp_task_{USER_TASK}",
        "tokens": 30_000,
    }
    assert sum(c["attempts"] for d in report["days"].values() for c in d.values()) == 7


def test_sessions_that_get_no_more_turns_are_not_live(agent_db, monkeypatch):
    """Sep 30: a finished guide's 170k-token session paged Kirill as a live context."""
    done, running = "5d3c2a10-0000-4000-8000-000000000001", "5d3c2a10-0000-4000-8000-000000000002"
    monkeypatch.setattr(um, "_ended_task_ids_sync", lambda ids: {done} & set(ids))
    agent_db.session("t-done", f"agent:main:rmp_task_{done}")
    agent_db.session("v-done", f"agent:main:rmp_verify_{done}")
    agent_db.session("in", "agent:main:rmp_intake_abc123_def456")
    agent_db.session("t-run", f"agent:main:rmp_task_{running}")
    agent_db.turn("t-done", NOW_MS - HOUR_MS, prompt=170_000)
    agent_db.turn("v-done", NOW_MS - HOUR_MS, prompt=90_000)
    agent_db.turn("in", NOW_MS - HOUR_MS, prompt=80_000)
    agent_db.turn("t-run", NOW_MS - HOUR_MS, prompt=40_000)

    report = um.transcript_usage(hours=24, now_ms=NOW_MS)

    assert report["max_live_context"] == {"session_key": f"agent:main:rmp_task_{running}", "tokens": 40_000}
    assert um.usage_alerts(report, input_budget=10_000_000) == []


def test_aborted_prompt_falls_back_to_the_previous_success(agent_db):
    agent_db.session("in", "agent:main:rmp_intake_abc_def")
    agent_db.turn("in", NOW_MS - 2 * HOUR_MS, prompt=8_000)
    agent_db.turn("in", NOW_MS - HOUR_MS, stop="aborted")
    report = um.transcript_usage(hours=24, now_ms=NOW_MS)
    assert report["by_category"]["intake"]["aborted_prompt_tokens"] == 8_000


def test_transcript_usage_without_a_store_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(um, "AGENT_DB_PATH", tmp_path / "missing.sqlite")
    report = um.transcript_usage(hours=24)
    assert report["available"] is False
    assert um.usage_alerts(report) == []


def _report(hours, *, prompt=0, attempts=0, aborted=0, ctx=0):
    return {
        "available": True,
        "window_hours": hours,
        "totals": {**um._zero_attribution(), "input_tokens": prompt,
                   "attempts": attempts, "aborted": aborted},
        "abort_rate": (aborted / attempts) if attempts else 0.0,
        "max_live_context": {"session_key": "agent:main:x", "tokens": ctx},
    }


def test_usage_alerts_need_the_breach_to_still_be_happening():
    day = _report(24, prompt=6_000_000, attempts=40, aborted=10)
    burning = _report(6, prompt=2_000_000, attempts=10, aborted=3)
    stopped = _report(6, prompt=50_000, attempts=10, aborted=0)
    alerts = um.usage_alerts(day, recent=burning, input_budget=5_000_000)
    assert any("over budget 5,000,000" in a for a in alerts)
    assert any(a.startswith("abort rate 25%") for a in alerts)
    assert um.usage_alerts(day, recent=stopped, input_budget=5_000_000) == []
    assert len(um.usage_alerts(day, input_budget=5_000_000)) == 2


def test_usage_alerts_flag_a_large_live_context_and_ignore_small_samples():
    assert um.usage_alerts(_report(24, ctx=61_000), recent=_report(6), input_budget=10**9)
    few = _report(24, attempts=10, aborted=9)
    assert um.usage_alerts(few, recent=_report(6, attempts=10, aborted=9), input_budget=10**9) == []


def test_scrape_reads_sqlite_transcripts_once_and_resumes(agent_db, monkeypatch, tmp_path):
    usage_file = tmp_path / "llm_usage.json"
    monkeypatch.setattr(um, "USAGE_PATH", usage_file)
    monkeypatch.setattr(um, "CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(um, "LOCK_PATH", tmp_path / ".llm_usage.lock")
    monkeypatch.setattr(um, "_profile_for_session_key", lambda key: None)
    now = time.time()
    agent_db.session("tk", f"agent:main:rmp_task_{USER_TASK}")
    agent_db.turn("tk", int((now - 30 * 3600) * 1000), prompt=999, msg_id="old")
    agent_db.turn("tk", int((now - 60) * 1000), prompt=1_000, output=10, msg_id="new-1")

    # The first run starts 24 h back, so the 30 h old turn is not imported.
    assert um.scrape_openclaw_sessions()["new_events"] == 1
    assert um.scrape_openclaw_sessions()["new_events"] == 0

    agent_db.turn("tk", int(now * 1000), prompt=2_000, output=20, msg_id="new-2")
    assert um.scrape_openclaw_sessions()["new_events"] == 1
    store = json.loads(usage_file.read_text())
    openai_days = [
        day["profiles"]["openai:default"]["totals"]
        for day in store["days"].values()
        if "openai:default" in day["profiles"]
    ]
    assert sum(t["requests"] for t in openai_days) == 2
    assert sum(t["input_tokens"] for t in openai_days) == 3_000
