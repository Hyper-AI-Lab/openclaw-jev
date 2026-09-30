"""Online backups of OpenClaw's stores: consistent while written, verified, and restorable."""
import json
import os
import sqlite3
import stat

import pytest

from ops import backup_openclaw_state as bos


def live_store(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS transcript_events (session_id TEXT, seq INTEGER, event_json TEXT)")
    con.executemany("INSERT INTO transcript_events VALUES (?, ?, ?)", [("s", n, f'{{"n": {n}}}') for n in rows])
    con.commit()
    return con


def count(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM transcript_events").fetchone()[0]
    finally:
        con.close()


@pytest.fixture
def home(tmp_path):
    return tmp_path / "openclaw"


def test_a_backup_is_a_consistent_checked_snapshot_while_the_gateway_writes(home, tmp_path):
    stores = bos.stores(home)
    writer = live_store(stores["agent"], range(50))
    state = live_store(stores["state"], range(3))
    writer.execute("INSERT INTO transcript_events VALUES ('s', 999, '{}')")  # not committed yet
    manifest = bos.backup(tmp_path / "b1", home=home, version="OpenClaw 2026.9.1")
    writer.rollback()
    assert set(manifest["files"]) == {"agent", "state"}
    assert manifest["openclaw_version"] == "OpenClaw 2026.9.1"
    assert all(meta["quick_check"] == "ok" for meta in manifest["files"].values())
    assert count(tmp_path / "b1" / "openclaw-agent.sqlite") == 50
    assert count(tmp_path / "b1" / "openclaw.sqlite") == 3
    assert stat.S_IMODE((tmp_path / "b1").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "b1" / "openclaw-agent.sqlite").stat().st_mode) == 0o600
    assert json.loads((tmp_path / "b1" / "manifest.json").read_text()) == manifest
    assert bos.verify(tmp_path / "b1") == manifest
    assert sorted(p.name for p in (tmp_path / "b1").iterdir()) == [
        "manifest.json", "openclaw-agent.sqlite", "openclaw.sqlite"]
    writer.close()
    state.close()


def test_a_tampered_backup_fails_verification(home, tmp_path):
    live_store(bos.stores(home)["agent"], range(5)).close()
    bos.backup(tmp_path / "b", home=home, version="x")
    copy = tmp_path / "b" / "openclaw-agent.sqlite"
    data = bytearray(copy.read_bytes())
    data[-1] ^= 0xFF
    copy.write_bytes(bytes(data))
    with pytest.raises(RuntimeError, match="does not match its manifest checksum"):
        bos.verify(tmp_path / "b")


def test_restore_replaces_the_stores_and_drops_a_stale_wal(home, tmp_path):
    agent = bos.stores(home)["agent"]
    live_store(agent, range(10)).close()
    os.chmod(agent, 0o640)
    bos.backup(tmp_path / "b", home=home, version="x")
    later = live_store(agent, range(10, 30))
    later.close()
    agent.with_name(agent.name + "-wal").write_bytes(b"stale wal from the newer version")
    agent.with_name(agent.name + "-shm").write_bytes(b"stale shm")
    manifest = bos.restore(tmp_path / "b", home=home, check_gateway=False)
    assert set(manifest["files"]) == {"agent"}
    assert count(agent) == 10
    assert not agent.with_name(agent.name + "-wal").exists() or agent.with_name(agent.name + "-wal").stat().st_size == 0
    assert stat.S_IMODE(agent.stat().st_mode) == 0o640


def test_restore_refuses_while_the_gateway_runs_and_without_confirmation(home, tmp_path, monkeypatch):
    live_store(bos.stores(home)["agent"], range(2)).close()
    bos.backup(tmp_path / "b", home=home, version="x")
    monkeypatch.setattr(bos, "gateway_active", lambda: True)
    with pytest.raises(RuntimeError, match="stop openclaw-gateway"):
        bos.restore(tmp_path / "b", home=home)
    assert bos.main(["restore", str(tmp_path / "b")]) == 2
