"""An OpenClaw 2026.9.7 agent store for tests: its schema, with events stored the way it stores them."""
import json
import sqlite3

import zstandard

SCHEMA_97 = """
CREATE TABLE session_windows (
  session_id TEXT NOT NULL PRIMARY KEY, session_key TEXT NOT NULL, previous_session_id TEXT, reason TEXT);
CREATE TABLE session_nodes (
  session_key TEXT NOT NULL PRIMARY KEY, current_session_id TEXT NOT NULL, entry_json TEXT NOT NULL,
  snapshot_revision INTEGER, entry_valid INTEGER, updated_at INTEGER, status TEXT, archived_at INTEGER,
  created_at INTEGER);
CREATE TABLE transcript_events (
  session_id TEXT NOT NULL, seq INTEGER NOT NULL, event_json TEXT, created_at INTEGER NOT NULL,
  event_zstd BLOB, event_utf8_bytes INTEGER CHECK (event_utf8_bytes IS NULL OR event_utf8_bytes >= 0),
  navigation_json TEXT,
  PRIMARY KEY (session_id, seq),
  CHECK ((event_json IS NOT NULL AND event_zstd IS NULL)
      OR (event_json IS NULL AND event_zstd IS NOT NULL AND event_utf8_bytes IS NOT NULL)));
"""
MIN_COMPRESS_BYTES = 1024


def compress_like_openclaw(text: str):
    """2026.9.7's prepareTranscriptPayload: zstd level 1 with a checksum, only when it saves enough."""
    raw = text.encode("utf-8")
    if len(raw) < MIN_COMPRESS_BYTES or "\\u" in text or "\0" in text:
        return None
    frame = zstandard.ZstdCompressor(level=1, write_checksum=True, write_content_size=True).compress(raw)
    if len(frame) > len(raw) - max(64, -(-len(raw) // 10)):
        return None
    return frame


class Store97:
    def __init__(self, path):
        self.path = path
        self.con = sqlite3.connect(path)
        self.con.executescript(SCHEMA_97)
        self.seq = {}
        self.compressed = 0

    def session(self, key, session_id, *, status="running", archived=False, entry=None):
        entry = {"sessionId": session_id, "status": status, "updatedAt": 1000, **(entry or {})}
        self.con.execute("INSERT INTO session_windows VALUES (?, ?, NULL, NULL)", (session_id, key))
        self.con.execute("INSERT INTO session_nodes VALUES (?, ?, ?, 0, 1, 1000, ?, ?, 1000)",
                         (key, session_id, json.dumps(entry), status, 1000 if archived else None))
        self.con.commit()

    def event(self, session_id, event, created_at=1000):
        seq = self.seq.get(session_id, 0) + 1
        self.seq[session_id] = seq
        text = json.dumps(event)
        frame = compress_like_openclaw(text)
        if frame is None:
            self.con.execute("INSERT INTO transcript_events (session_id, seq, event_json, created_at) "
                             "VALUES (?, ?, ?, ?)", (session_id, seq, text, created_at))
        else:
            self.compressed += 1
            self.con.execute(
                "INSERT INTO transcript_events (session_id, seq, event_json, created_at, event_zstd, "
                "event_utf8_bytes, navigation_json) VALUES (?, ?, NULL, ?, ?, ?, ?)",
                (session_id, seq, created_at, frame, len(text.encode("utf-8")),
                 json.dumps({"report": {"type": event.get("type")}})))
        self.con.commit()
