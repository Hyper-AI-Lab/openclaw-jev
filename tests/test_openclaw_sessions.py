import json
import sqlite3
from pathlib import Path

from app import openclaw_sessions as osess


def _make_agent_db(tmp_path: Path) -> Path:
    db = tmp_path / "openclaw-agent.sqlite"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE session_nodes ("
        "session_key TEXT PRIMARY KEY, current_session_id TEXT, entry_json TEXT, "
        "entry_valid INTEGER, updated_at INTEGER, status TEXT)"
    )
    con.execute(
        "CREATE TABLE transcript_events ("
        "session_id TEXT, seq INTEGER, event_json TEXT, created_at INTEGER)"
    )
    entry = {
        "sessionId": "sid-1",
        "status": "running",
        "updatedAt": 1000,
        "origin": {"from": "slack:channel:U0AELFYTLKS"},
    }
    con.execute(
        "INSERT INTO session_nodes VALUES (?,?,?,?,?,?)",
        ("agent:main:rmp_intake_x", "sid-1", json.dumps(entry), 1, 1000, "running"),
    )
    event = {
        "type": "message",
        "timestamp": "2026-09-05T00:00:01Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    }
    con.execute(
        "INSERT INTO transcript_events VALUES (?,?,?,?)",
        ("sid-1", 1, json.dumps(event), 1),
    )
    con.commit()
    con.close()
    return db


def test_sqlite_session_and_transcript(monkeypatch, tmp_path):
    db = _make_agent_db(tmp_path)
    monkeypatch.setattr(osess, "AGENT_DB_PATH", db)
    monkeypatch.setattr(osess, "SESSIONS_JSON_PATH", tmp_path / "missing-sessions.json")
    monkeypatch.setattr(osess, "SESSIONS_DIR", tmp_path)

    entry = osess.get_session_entry("agent:main:rmp_intake_x")
    assert entry["sessionId"] == "sid-1"
    assert entry["status"] == "running"
    lines = osess.read_transcript_lines("sid-1")
    assert len(lines) == 1
    assert "hello" in lines[0]
    assert osess.patch_session_entry(
        "agent:main:rmp_intake_x",
        {"authProfileOverride": "nvidia:key2"},
    )
    assert osess.get_session_entry("agent:main:rmp_intake_x")["authProfileOverride"] == "nvidia:key2"


def test_json_session_fallback(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions.json"
    sessions.write_text(
        json.dumps({"agent:main:main": {"sessionId": "json-sid", "status": "done"}})
    )
    monkeypatch.setattr(osess, "SESSIONS_JSON_PATH", sessions)
    entry = osess.get_session_entry("agent:main:main", str(sessions))
    assert entry["sessionId"] == "json-sid"
