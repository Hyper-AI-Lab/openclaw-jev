import importlib.util
import json
import sqlite3
from pathlib import Path

_SETTLE = Path(__file__).resolve().parents[1] / "ops" / "settle_openclaw_sessions.py"
_spec = importlib.util.spec_from_file_location("settle_openclaw_sessions", _SETTLE)
_mod = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(_mod)
settle = _mod.settle


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "openclaw-agent.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE session_nodes (
          session_key TEXT PRIMARY KEY,
          current_session_id TEXT,
          entry_json TEXT,
          entry_valid INTEGER,
          updated_at INTEGER
        );
        CREATE TABLE session_windows (
          session_id TEXT PRIMARY KEY,
          session_key TEXT
        );
        """
    )
    good = {"sessionId": "sid-a", "updatedAt": 100}
    drifted = {"sessionId": "sid-b", "updatedAt": 200}
    con.execute(
        "INSERT INTO session_nodes VALUES (?,?,?,?,?)",
        ("agent:main:rmp_intake_a", "sid-a", json.dumps(good), 0, 100),
    )
    con.execute(
        "INSERT INTO session_nodes VALUES (?,?,?,?,?)",
        ("agent:main:rmp_intake_b", "sid-b", json.dumps(drifted), -1, 999),
    )
    con.execute(
        "INSERT INTO session_nodes VALUES (?,?,?,?,?)",
        ("agent:main:orphan", "sid-x", "{}", -1, 1),
    )
    con.execute(
        "INSERT INTO session_nodes VALUES (?,?,?,?,?)",
        ("agent:main:placeholder", "sid-p", "{}", -1, 1),
    )
    con.execute(
        "INSERT INTO session_windows VALUES (?,?)",
        ("sid-p", "agent:main:placeholder"),
    )
    con.commit()
    con.close()
    return db


def test_settle_marks_parseable_and_keeps_windowed_placeholder(tmp_path):
    db = _db(tmp_path)
    result = settle(db)
    assert result["ok"] is True
    assert result["fixed"] == 2
    assert result["deleted_placeholders"] == 1
    con = sqlite3.connect(str(db))
    rows = {
        r[0]: r[1:]
        for r in con.execute(
            "SELECT session_key, entry_valid, updated_at FROM session_nodes"
        )
    }
    assert rows["agent:main:rmp_intake_a"][0] == 1
    assert rows["agent:main:rmp_intake_b"] == (1, 200)
    assert "agent:main:orphan" not in rows
    assert rows["agent:main:placeholder"][0] == -1
    triggers = {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='session_nodes'"
        )
    }
    assert "session_nodes_entry_valid_after_insert" in triggers
    con.close()
