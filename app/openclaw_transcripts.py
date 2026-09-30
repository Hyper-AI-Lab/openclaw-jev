"""Transcript events in OpenClaw's agent database, however OpenClaw stored them.

OpenClaw 2026.9.1 keeps every event as JSON text in ``transcript_events.event_json``. From
2026.9.7 an event of 1 KiB or more is stored zstd-compressed in ``event_zstd`` (level 1, with a
checksum), with ``event_json`` NULL and its UTF-8 length in ``event_utf8_bytes``; OpenClaw's own
readers decode through a SQLite function that only OpenClaw registers. Every RMP reader of
transcript events selects through ``event_columns`` and decodes with ``decode_event``.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

import zstandard

logger = logging.getLogger("rmp.openclaw_transcripts")

# OpenClaw's MAX_COMPRESSED_EVENT_BYTES: no stored event is larger.
MAX_EVENT_BYTES = 4 * 1024 * 1024
_PLAIN = ("event_json", None, None)
_COMPRESSED = ("event_json", "event_zstd", "event_utf8_bytes")


def stores_compressed_events(con: sqlite3.Connection) -> bool:
    return any(row[1] == "event_zstd" for row in con.execute("PRAGMA table_info(transcript_events)"))


def event_columns(con: sqlite3.Connection, alias: str = "") -> str:
    """The SELECT list yielding (json, zstd, utf8_bytes) on either schema."""
    prefix = f"{alias}." if alias else ""
    columns = _COMPRESSED if stores_compressed_events(con) else _PLAIN
    return ", ".join(f"{prefix}{name}" if name else "NULL" for name in columns)


def decode_event(event_json: Optional[str], event_zstd: Optional[bytes], utf8_bytes: Optional[int]) -> Optional[str]:
    """The event's JSON text; None when it has none or cannot be decoded."""
    if event_json is not None:
        return event_json
    if event_zstd is None:
        return None
    limit = int(utf8_bytes) if utf8_bytes else MAX_EVENT_BYTES
    try:
        # A decompressor per call: zstandard's contexts are not thread-safe.
        raw = zstandard.ZstdDecompressor().decompress(bytes(event_zstd), max_output_size=limit)
        return raw.decode("utf-8")
    except (zstandard.ZstdError, UnicodeDecodeError, ValueError) as exc:
        logger.warning("Undecodable transcript event skipped: %s", exc)
        return None
