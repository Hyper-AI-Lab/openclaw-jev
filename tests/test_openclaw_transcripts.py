"""RMP reads transcripts stored the 2026.9.7 way (zstd events) as it read 2026.9.1's plain ones."""
import base64
import json
import shutil
import sqlite3
import subprocess

import pytest

from app import openclaw_sessions as osess
from app import openclaw_transcripts as ot
from app.activities.openclaw_activities import _poll_jsonl_for_response
from app.llm import usage_monitor as um
from app.orchestrator import session_recovery
from app.production.canary_sentinel import inspect_memory_canary_transcript
from tests.openclaw97 import Store97, compress_like_openclaw

PAD = "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor. " * 30
TASK = "5d3c2a10-0000-4000-8000-00000000abcd"


def text_event(role, text, *, at=2000, stop=None, **message):
    msg = {"role": role, "content": [{"type": "text", "text": text}], **message}
    if stop:
        msg["stopReason"] = stop
    return {"type": "message", "timestamp": at, "message": msg}


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = Store97(tmp_path / "openclaw-agent.sqlite")
    monkeypatch.setattr(osess, "AGENT_DB_PATH", s.path)
    monkeypatch.setattr(osess, "SESSIONS_JSON_PATH", tmp_path / "missing-sessions.json")
    monkeypatch.setattr(osess, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(um, "AGENT_DB_PATH", s.path)
    yield s
    s.con.close()


def test_plain_events_pass_through_and_openclaw_frames_decode():
    assert ot.decode_event('{"a": 1}', None, None) == '{"a": 1}'
    text = json.dumps({"pad": PAD})
    frame = compress_like_openclaw(text)
    assert frame is not None and ot.decode_event(None, frame, len(text.encode())) == text
    assert ot.decode_event(None, frame, None) == text
    assert ot.decode_event(None, b"not a zstd frame", 16) is None
    assert ot.decode_event(None, None, None) is None


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_frames_compressed_by_nodes_zlib_decode():
    """OpenClaw compresses with node:zlib; RMP decodes with zstandard."""
    text = json.dumps({"pad": PAD})
    js = ("const z = require('zlib');"
          "if (!z.zstdCompressSync) { process.exit(3); }"
          "const c = z.zstdCompressSync(Buffer.from(process.argv[1]), {params: {"
          "[z.constants.ZSTD_c_compressionLevel]: 1, [z.constants.ZSTD_c_checksumFlag]: 1}});"
          "process.stdout.write(c.toString('base64'));")
    run = subprocess.run(["node", "-e", js, text], capture_output=True, text=True)
    if run.returncode == 3:
        pytest.skip("this node has no zstd")
    assert run.returncode == 0, run.stderr
    assert ot.decode_event(None, base64.b64decode(run.stdout), len(text.encode())) == text


def test_the_91_schema_reads_as_before(tmp_path, monkeypatch):
    path = tmp_path / "agent.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, event_json TEXT NOT NULL, created_at INTEGER)")
    con.execute("CREATE TABLE session_nodes (session_key TEXT PRIMARY KEY, current_session_id TEXT, entry_json TEXT)")
    con.executemany("INSERT INTO transcript_events VALUES (?, ?, ?, ?)",
                    [("s", 2, '{"n": 2}', 2), ("s", 1, '{"n": 1}', 1), ("other", 1, '{"n": 9}', 1)])
    con.commit()
    assert ot.event_columns(con) == "event_json, NULL, NULL"
    assert ot.event_columns(con, "e") == "e.event_json, NULL, NULL"
    con.close()
    monkeypatch.setattr(osess, "AGENT_DB_PATH", path)
    monkeypatch.setattr(osess, "SESSIONS_JSON_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(osess, "SESSIONS_DIR", tmp_path)
    assert osess.read_transcript_lines("s") == ['{"n": 1}', '{"n": 2}']


def test_reply_polling_finds_its_marker_in_a_compressed_turn(store):
    """The dispatch marker rides in a long prompt, which 2026.9.7 compresses."""
    store.session(f"agent:main:rmp_task_{TASK}", "sid-1")
    marker = "[RMP_DISPATCH abc123def456]"
    store.event("sid-1", text_event("user", f"{marker}\n{PAD}", at=2000))
    store.event("sid-1", text_event("assistant", "Here is the finished comparison. " + PAD, at=3000, stop="stop"))
    assert store.compressed == 2
    con = sqlite3.connect(store.path)
    assert ot.event_columns(con, "e") == "e.event_json, e.event_zstd, e.event_utf8_bytes"
    con.close()
    lines = osess.read_transcript_lines("sid-1")
    assert len(lines) == 2 and marker in lines[0]
    text, reason, _ = _poll_jsonl_for_response("", 1000, lines, marker)
    assert text.startswith("Here is the finished comparison.") and reason == "stop"


def test_recovery_tool_results_and_canary_read_compressed_history(store):
    key = f"agent:main:rmp_task_{TASK}"
    store.session(key, "sid-2", status="done")
    store.event("sid-2", text_event("user", "Remember: do not use memory_search. Use process-scoped memory. " + PAD))
    store.event("sid-2", {"type": "message", "timestamp": 2100, "message": {
        "role": "assistant", "stopReason": "toolUse",
        "content": [{"type": "toolCall", "id": "call-1", "name": "web_fetch", "arguments": {"url": "https://x.example"}}]}})
    store.event("sid-2", {"type": "message", "timestamp": 2200, "message": {
        "role": "toolResult", "toolCallId": "call-1", "content": [{"type": "text", "text": PAD * 2}]}})
    store.event("sid-2", text_event("assistant", "The page covers the release schedule in detail. " + PAD,
                                    at=2300, stop="stop"))
    assert store.compressed == 3
    reply = session_recovery.read_completed_rmp_session_reply(TASK)
    assert reply and reply.startswith("The page covers the release schedule")
    results = osess.task_tool_results(TASK, tools={"web_fetch"}, min_chars=2000)
    assert [(r["tool"], r["arguments"]["url"], len(r["text"])) for r in results] == [
        ("web_fetch", "https://x.example", len(PAD * 2))]
    canary = inspect_memory_canary_transcript(TASK)
    assert canary["has_transcript"] and canary["memory_ok"] == 1 and canary["prompt_ok"] == 1


def test_usage_accounting_counts_compressed_turns(store, tmp_path, monkeypatch):
    monkeypatch.setattr(um, "USAGE_PATH", tmp_path / "llm_usage.json")
    monkeypatch.setattr(um, "CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(um, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(um, "_task_kinds_sync", lambda ids: {TASK: "user"})
    monkeypatch.setattr(um, "_ended_task_ids_sync", lambda ids: set())
    store.session(f"agent:main:rmp_task_{TASK}", "sid-3")
    now_ms = int(um.time.time() * 1000)  # the scrape counts only the last day
    for n, (prompt, output, long) in enumerate([(30_000, 400, True), (32_000, 300, False)]):
        at = now_ms - (2 - n) * 3_600_000
        store.event("sid-3", text_event(
            "assistant", ("A long answer. " + PAD) if long else "Short.", at=at, stop="stop",
            provider="openai", model="gpt-6-luna",
            usage={"input": prompt, "cacheRead": 0, "output": output, "totalTokens": prompt + output},
            timestamp=at), created_at=at)
    assert store.compressed == 1
    report = um.transcript_usage(hours=24, now_ms=now_ms)
    assert report["totals"]["attempts"] == 2 and report["totals"]["input_tokens"] == 62_000
    assert report["max_live_context"] == {"session_key": f"agent:main:rmp_task_{TASK}", "tokens": 32_000}
    scraped = um.scrape_openclaw_sessions()
    assert scraped["scanned_events"] == 2 and scraped["source"] == "sqlite"


def test_history_imported_at_new_rowids_is_not_counted_again(store, tmp_path, monkeypatch):
    """Oct 1: 2026.9.7's migration imported 107k JSONL-era events (Feb-Sep) at rowids after the cursor."""
    monkeypatch.setattr(um, "USAGE_PATH", tmp_path / "llm_usage.json")
    monkeypatch.setattr(um, "CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(um, "LOCK_PATH", tmp_path / ".lock")
    now_ms = int(um.time.time() * 1000)
    store.session(f"agent:main:rmp_task_{TASK}", "sid-4")

    def assistant(at, msg_id):
        return {**text_event("assistant", "ok", at=at, stop="stop", provider="openai", model="gpt-6-luna",
                             usage={"input": 100, "cacheRead": 0, "output": 5, "totalTokens": 105}, timestamp=at),
                "id": msg_id}

    store.event("sid-4", assistant(now_ms - 3_600_000, "live-1"), created_at=now_ms - 3_600_000)
    assert um.scrape_openclaw_sessions()["new_events"] == 1
    for n in range(3):
        old = now_ms - (120 + n) * 86_400_000
        store.event("sid-4", assistant(old, f"imported-{n}"), created_at=old)
    store.event("sid-4", assistant(now_ms - 60_000, "live-2"), created_at=now_ms - 60_000)
    scraped = um.scrape_openclaw_sessions()
    assert (scraped["scanned_events"], scraped["new_events"]) == (1, 1)
