"""Backups and a clock for the migration tests (specs/WP-26.md WP26-05): a plain helper module, no fixtures.

``NOON`` is outside 03:00-03:45 UTC, the backup window in which the migration wrapper writes nothing, and
``make_backup`` builds a complete backup of any age, so a test's write meets a usable backup whatever the hour the
suite runs.
"""
import io
import json
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from app.production import backup_manifest

NOON = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
TEMPORAL_DBS = ("temporal", "temporal_visibility")


def _targz(path: Path, top: str, files: dict) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def make_backup(root, *, age_h: float, now: datetime = NOON, failures: Sequence[str] = ()) -> Path:
    """A complete backup under ``root`` as ops/backup.sh leaves one (Qdrant as a server, the artifact store on),
    stamped ``age_h`` hours before ``now``, its manifest written with ``failures``. Returns its directory."""
    dest = Path(root) / (now - timedelta(hours=age_h)).strftime("%Y%m%dT%H%M%SZ")
    dest.mkdir(parents=True)
    for db in ("rmp_db", *TEMPORAL_DBS):
        (dest / f"{db}.dump").write_bytes(b"PGDMP" + db.encode())
    (dest / "qdrant-full.snapshot").write_bytes(b"snapshot")
    _targz(dest / "artifacts.tar.gz", "artifacts", {"a.json": b"{}"})
    state = dest / backup_manifest.OPENCLAW_STATE
    state.mkdir()
    stores = {"agent": "openclaw-agent.sqlite", "state": "openclaw.sqlite"}
    for store, name in stores.items():
        (state / name).write_bytes(f"SQLite format 3 {store}".encode())
    (state / "manifest.json").write_text(json.dumps({"files": {store: {"file": name} for store, name in stores.items()}}))
    _targz(dest / "workspace.tar.gz", "workspace", {"AGENTS.md": b"# Aura"})
    (dest / "settings.json").write_text("{}")
    (dest / "openclaw.json").write_text("{}")
    backup_manifest.write(dest, failures=list(failures), temporal_dbs=list(TEMPORAL_DBS), qdrant_mode="server",
                          artifacts_enabled=True, read_back=lambda path: True)
    return dest
