#!/usr/bin/env python3
"""Settle OpenClaw 2026.9 session_nodes.entry_valid without dropping schema triggers.

Pending (0) or invalid (-1) rows poison every /hooks/agent scan. Doctor's own
maintenance can mark parseable rows -1 when updated_at drifts from JSON.
Never drop the entry_valid triggers — gateway refuses to boot without them.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("/root/.openclaw/agents/main/agent/openclaw-agent.sqlite")

TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS session_nodes_entry_valid_after_insert
AFTER INSERT ON session_nodes
BEGIN
  UPDATE session_nodes SET entry_valid = 0 WHERE session_key = NEW.session_key;
END;
CREATE TRIGGER IF NOT EXISTS session_nodes_entry_valid_after_entry_update
AFTER UPDATE OF entry_json ON session_nodes
BEGIN
  UPDATE session_nodes SET entry_valid = 0 WHERE session_key = NEW.session_key;
END;
CREATE TRIGGER IF NOT EXISTS session_nodes_entry_valid_after_identity_update
AFTER UPDATE OF current_session_id, updated_at ON session_nodes
BEGIN
  UPDATE session_nodes SET entry_valid = 0 WHERE session_key = NEW.session_key;
END;
"""


def settle(db_path: Path = DB_PATH) -> dict:
    if not db_path.is_file():
        return {"ok": False, "error": f"missing {db_path}"}
    con = sqlite3.connect(str(db_path), timeout=15)
    con.execute("PRAGMA busy_timeout=15000")
    con.executescript(TRIGGERS_SQL)
    trigger_names = [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='session_nodes' ORDER BY 1"
        )
    ]
    deleted_placeholders = con.execute(
        """
        DELETE FROM session_nodes
        WHERE entry_json = '{}'
          AND NOT EXISTS (
            SELECT 1 FROM session_windows w
            WHERE w.session_key = session_nodes.session_key
              AND w.session_id = session_nodes.current_session_id
          )
        """
    ).rowcount
    fixed = 0
    skipped = 0
    rows = con.execute(
        "SELECT session_key, current_session_id, entry_json, entry_valid, updated_at "
        "FROM session_nodes WHERE entry_valid != 1"
    ).fetchall()
    for key, sid, blob, valid, upd in rows:
        try:
            entry = json.loads(blob or "")
        except json.JSONDecodeError:
            entry = None
        if blob == "{}":
            skipped += 1
            continue
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("sessionId"), str)
            and isinstance(entry.get("updatedAt"), (int, float))
            and entry.get("sessionId") == sid
        ):
            # Align column timestamp before marking valid=1. Updating updated_at
            # fires the identity trigger (valid=0), so set valid=1 afterwards.
            want_upd = int(entry["updatedAt"])
            if upd != want_upd:
                con.execute(
                    "UPDATE session_nodes SET updated_at = ? WHERE session_key = ?",
                    (want_upd, key),
                )
            con.execute(
                "UPDATE session_nodes SET entry_valid = 1 WHERE session_key = ?",
                (key,),
            )
            fixed += 1
        else:
            skipped += 1
    con.commit()
    invalid_left = con.execute(
        "SELECT COUNT(*) FROM session_nodes WHERE entry_valid != 1"
    ).fetchone()[0]
    con.close()
    return {
        "ok": True,
        "db": str(db_path),
        "triggers": trigger_names,
        "deleted_placeholders": deleted_placeholders,
        "fixed": fixed,
        "skipped": skipped,
        "invalid_left": invalid_left,
    }


if __name__ == "__main__":
    result = settle(Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH)
    print(json.dumps(result))
    sys.exit(0 if result.get("ok") else 1)
