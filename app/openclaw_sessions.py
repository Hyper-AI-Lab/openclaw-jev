"""OpenClaw session store adapter (sessions.json or 2026.9+ SQLite)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.config import OPENCLAW_HOME, SESSIONS_JSON_PATH as _SESSIONS_JSON

SESSIONS_JSON_PATH = Path(_SESSIONS_JSON)
AGENT_DB_PATH = Path(OPENCLAW_HOME) / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
SESSIONS_DIR = Path(OPENCLAW_HOME) / "agents" / "main" / "sessions"


def _connect(db: Path, readonly: bool) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    else:
        con = sqlite3.connect(str(db), timeout=10)
        con.execute("PRAGMA busy_timeout = 10000")
    return con


def _sqlite_sessions_available(db: Optional[Path] = None) -> bool:
    if db is None:
        db = AGENT_DB_PATH
    if not db.is_file():
        return False
    try:
        con = _connect(db, readonly=True)
        try:
            row = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_nodes'"
            ).fetchone()
            return row is not None
        finally:
            con.close()
    except Exception:
        return False


def _entry_from_node(
    current_session_id: Optional[str],
    status: Optional[str],
    entry_json: Optional[str],
    updated_at: Optional[int],
) -> Dict[str, Any]:
    try:
        entry = json.loads(entry_json) if entry_json else {}
    except Exception:
        entry = {}
    if not isinstance(entry, dict):
        entry = {}
    if current_session_id:
        entry.setdefault("sessionId", current_session_id)
    if status:
        entry.setdefault("status", status)
    if updated_at and "updatedAt" not in entry:
        entry["updatedAt"] = updated_at
    return entry


def get_session_entry(
    session_key: str,
    sessions_json_path: Optional[str] = None,
) -> Dict[str, Any]:
    path = Path(sessions_json_path or SESSIONS_JSON_PATH)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entry = data.get(session_key) if isinstance(data, dict) else None
            return dict(entry) if isinstance(entry, dict) else {}
        except Exception:
            return {}
    if not session_key or not _sqlite_sessions_available():
        return {}
    try:
        con = _connect(AGENT_DB_PATH, readonly=True)
        try:
            row = con.execute(
                "SELECT current_session_id, status, entry_json, updated_at "
                "FROM session_nodes WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return {}
    if not row:
        return {}
    return _entry_from_node(*row)


def iter_session_entries(
    sessions_json_path: Optional[str] = None,
) -> Iterable[Tuple[str, Dict[str, Any]]]:
    path = Path(sessions_json_path or SESSIONS_JSON_PATH)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(data, dict):
            for key, entry in data.items():
                if isinstance(entry, dict):
                    yield str(key), entry
        return
    if not _sqlite_sessions_available():
        return
    try:
        con = _connect(AGENT_DB_PATH, readonly=True)
        try:
            rows = con.execute(
                "SELECT session_key, current_session_id, status, entry_json, updated_at "
                "FROM session_nodes"
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return
    for session_key, current_session_id, status, entry_json, updated_at in rows:
        yield session_key, _entry_from_node(
            current_session_id, status, entry_json, updated_at
        )


def patch_session_entry(
    session_key: str,
    updates: Dict[str, Any],
    sessions_json_path: Optional[str] = None,
) -> bool:
    """Merge updates into an existing session entry. Does not create a new key."""
    if not session_key or not updates:
        return False
    path = Path(sessions_json_path or SESSIONS_JSON_PATH)
    if path.is_file():
        try:
            store = json.loads(path.read_text(encoding="utf-8"))
            entry = store.get(session_key)
            if not isinstance(entry, dict) or not entry.get("sessionId"):
                return False
            entry.update(updates)
            store[session_key] = entry
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
            return True
        except Exception:
            return False
    if not _sqlite_sessions_available():
        return False
    try:
        con = _connect(AGENT_DB_PATH, readonly=False)
        try:
            row = con.execute(
                "SELECT current_session_id, status, entry_json, updated_at "
                "FROM session_nodes WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            if not row:
                return False
            entry = _entry_from_node(*row)
            if not entry.get("sessionId"):
                return False
            entry.update(updates)
            now_ms = int(time.time() * 1000)
            con.execute(
                "UPDATE session_nodes SET entry_json = ?, current_session_id = ?, "
                "status = ?, updated_at = ? WHERE session_key = ?",
                (
                    json.dumps(entry, separators=(",", ":")),
                    entry.get("sessionId") or row[0],
                    entry.get("status") or row[1],
                    now_ms,
                    session_key,
                ),
            )
            con.commit()
            return True
        finally:
            con.close()
    except Exception:
        return False


def read_transcript_lines(session_id: str) -> List[str]:
    """JSONL-equivalent transcript lines for a session id."""
    if not session_id:
        return []
    jsonl = SESSIONS_DIR / f"{session_id}.jsonl"
    if jsonl.is_file():
        try:
            return jsonl.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return []
    if not _sqlite_sessions_available():
        return []
    try:
        con = _connect(AGENT_DB_PATH, readonly=True)
        try:
            rows = con.execute(
                "SELECT event_json FROM transcript_events WHERE session_id = ? "
                "ORDER BY CAST(seq AS INTEGER), seq",
                (session_id,),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    return [row[0] for row in rows if row and row[0]]
