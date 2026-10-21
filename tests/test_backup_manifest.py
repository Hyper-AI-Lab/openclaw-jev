"""app/production/backup_manifest.py: the format-2 manifest a backup writes, and the problems a reader finds in one.

Each test builds a backup directory in tmp_path as ops/backup.sh leaves it. The read-back is a stub, except where a
test proves the default one, whose pg_restore is a fake on PATH.
"""
import hashlib
import io
import json
import os
import shutil
import socket
import tarfile
from pathlib import Path

import pytest

from app.production import backup_manifest as bm

STAMP = "20261021T031502Z"
DBS = ["temporal", "temporal_visibility"]
COMPONENTS = {
    "postgres": ["rmp_db.dump"],
    "temporal": ["temporal.dump", "temporal_visibility.dump"],
    "qdrant": ["qdrant-full.snapshot"],
    "artifacts": ["artifacts.tar.gz"],
    "openclaw-state": ["openclaw-state/manifest.json", "openclaw-state/openclaw-agent.sqlite",
                       "openclaw-state/openclaw.sqlite"],
    "workspace": ["workspace.tar.gz"],
    "settings": ["settings.json", "openclaw.json"],
}
# Every expected file of a complete backup, with its component.
EXPECTED = [(name, component) for component, names in COMPONENTS.items() for name in names]

# pg_restore -f /dev/null <dump>: records its argv, and fails for the dump FAKE_PGRESTORE_FAIL names.
FAKE_PG_RESTORE = r'''#!/bin/sh
printf '%s\n' "$*" >> "${FAKE_STATE}/pg_restore.log"
for a in "$@"; do last="$a"; done
[ "$(basename "${last}")" = "${FAKE_PGRESTORE_FAIL:-}" ] && { echo "pg_restore: error: could not read block" >&2; exit 1; }
exit 0
'''


def _targz(path, top, files):
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def _openclaw_state(dest, stores=("agent", "state")):
    names = {"agent": "openclaw-agent.sqlite", "state": "openclaw.sqlite"}
    state = dest / "openclaw-state"
    state.mkdir()
    for store in stores:
        (state / names[store]).write_bytes(f"SQLite format 3 {store}".encode())
    (state / "manifest.json").write_text(json.dumps({"files": {store: {"file": names[store]} for store in stores}}))


@pytest.fixture
def dest(tmp_path):
    """A complete backup in server mode with the artifact store on."""
    dest = tmp_path / "backups" / STAMP
    dest.mkdir(parents=True)
    for db in ("rmp_db", *DBS):
        (dest / f"{db}.dump").write_bytes(b"PGDMP" + db.encode())
    (dest / "qdrant-full.snapshot").write_bytes(b"snapshot")
    _targz(dest / "artifacts.tar.gz", "artifacts", {"a.json": b"{}"})
    _openclaw_state(dest)
    _targz(dest / "workspace.tar.gz", "workspace", {"AGENTS.md": b"# Aura"})
    (dest / "settings.json").write_text("{}")
    (dest / "openclaw.json").write_text("{}")
    return dest


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(dest, read=None, **overrides):
    """write() as ops/backup.sh calls it for a whole run, with a stub read-back that records what it reads."""
    kwargs = {"failures": [], "temporal_dbs": DBS, "qdrant_mode": "server", "artifacts_enabled": True,
              "read_back": (lambda path: read.append(path.name) is None) if read is not None else (lambda path: True),
              **overrides}
    return bm.write(dest, **kwargs)


def test_a_complete_backup_gets_a_format_2_manifest_with_every_file_s_size_and_checksum(dest):
    read = []
    manifest = _write(dest, read)
    assert json.loads((dest / "manifest.json").read_text()) == manifest
    assert {key: manifest[key] for key in ("format", "timestamp", "host", "qdrant_mode", "failures")} == {
        "format": 2, "timestamp": STAMP, "host": socket.gethostname(), "qdrant_mode": "server", "failures": []}
    assert list(manifest["components"]) == list(bm.REQUIRED_COMPONENTS) and manifest["components"] == COMPONENTS
    assert manifest["files"] == {name: {"bytes": (dest / name).stat().st_size, "sha256": _sha256(dest / name)}
                                 for name, _ in EXPECTED}
    # Dumps and archives read back; the snapshot, the store copies and the config snapshots have their own checks.
    assert read == ["rmp_db.dump", "temporal.dump", "temporal_visibility.dump", "artifacts.tar.gz", "workspace.tar.gz"]
    assert not (dest / "manifest.json.partial").exists()
    assert bm.problems(dest, hashes=True, qdrant_mode="server") == []


@pytest.mark.parametrize("damage", ["missing", "empty", "unreadable"])
@pytest.mark.parametrize("name, component", EXPECTED, ids=[name for name, _ in EXPECTED])
def test_each_missing_or_empty_expected_file_adds_its_component_once(dest, monkeypatch, capsys, name, component,
                                                                     damage):
    path = dest / name
    if damage == "missing":
        path.unlink()
    elif damage == "empty":
        path.write_bytes(b"")
    else:
        sha256 = bm._sha256

        def unreadable(p):
            if p == path:
                raise PermissionError(13, "Permission denied")
            return sha256(p)

        monkeypatch.setattr(bm, "_sha256", unreadable)
    manifest = _write(dest)
    assert manifest["failures"] == [component]
    # The file is listed all the same, so a reader sees it as well as the failure.
    assert name in manifest["components"][component]
    assert (name in manifest["files"]) == (damage == "empty")
    reason = {"missing": "is missing", "empty": "is empty", "unreadable": "does not read (Permission denied)"}[damage]
    if name == "openclaw-state/manifest.json" and damage == "empty":
        reason = "does not read"
    assert f"FAIL: {component}: {name} {reason}" in capsys.readouterr().err


def test_a_component_fails_once_however_many_of_its_files_are_missing(dest, capsys):
    # Both config snapshots of a run can fail: the run's own failures are kept once each too.
    assert _write(dest, failures=["settings", "qdrant", "settings"])["failures"] == ["settings", "qdrant"]
    (dest / "temporal.dump").unlink()
    (dest / "temporal_visibility.dump").write_bytes(b"")
    (dest / "settings.json").unlink()
    (dest / "openclaw.json").unlink()
    assert _write(dest)["failures"] == ["temporal", "settings"]
    assert capsys.readouterr().err.count("FAIL: ") == 2
    # A component the run failed already, or one of its parts, is not added again.
    (dest / "workspace.tar.gz").unlink()
    manifest = _write(dest, failures=["temporal:temporal", "settings", "workspace"])
    assert manifest["failures"] == ["temporal:temporal", "settings", "workspace"]
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("stores, absent", [(("agent",), "state"), (("state",), "agent")])
def test_openclaw_state_without_either_store_fails(dest, capsys, stores, absent):
    shutil.rmtree(dest / "openclaw-state")
    _openclaw_state(dest, stores)
    manifest = _write(dest)
    assert manifest["failures"] == ["openclaw-state"]
    assert len(manifest["components"]["openclaw-state"]) == 2
    assert f"FAIL: openclaw-state: openclaw-state/manifest.json does not list {absent}" in capsys.readouterr().err


def test_a_dump_or_archive_that_does_not_read_back_fails_its_component(dest, tmp_path, monkeypatch):
    fake, state = tmp_path / "bin", tmp_path / "state"
    fake.mkdir()
    state.mkdir()
    (fake / "pg_restore").write_text(FAKE_PG_RESTORE)
    (fake / "pg_restore").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_STATE", str(state))
    assert shutil.which("pg_restore") == str(fake / "pg_restore"), "pg_restore does not resolve to its fake"
    assert bm.write(dest, failures=[], temporal_dbs=DBS, qdrant_mode="server", artifacts_enabled=True)["failures"] == []
    # pg_restore reads every block of each dump and writes its SQL nowhere.
    assert (state / "pg_restore.log").read_text().splitlines() == [
        f"-f /dev/null {dest / name}" for name in ("rmp_db.dump", "temporal.dump", "temporal_visibility.dump")]
    monkeypatch.setenv("FAKE_PGRESTORE_FAIL", "temporal.dump")
    assert bm.write(dest, failures=[], temporal_dbs=DBS, qdrant_mode="server",
                    artifacts_enabled=True)["failures"] == ["temporal:temporal"]
    monkeypatch.setenv("FAKE_PGRESTORE_FAIL", "rmp_db.dump")
    archive = dest / "artifacts.tar.gz"
    archive.write_bytes(archive.read_bytes()[:-8])
    assert bm.write(dest, failures=[], temporal_dbs=DBS, qdrant_mode="server",
                    artifacts_enabled=True)["failures"] == ["postgres", "artifacts"]
    monkeypatch.delenv("FAKE_PGRESTORE_FAIL")
    (dest / "workspace.tar.gz").write_bytes(b"not an archive")
    assert bm.write(dest, failures=[], temporal_dbs=DBS, qdrant_mode="server",
                    artifacts_enabled=True)["failures"] == ["artifacts", "workspace"]


def test_problems_reports_each_case(dest):
    _write(dest)
    assert bm.problems(dest, hashes=True) == []
    (dest / "settings.json").unlink()
    (dest / "openclaw.json").write_bytes(b"")
    (dest / "rmp_db.dump").write_bytes(b"PGDMP" + b"rmp_db" + b"!")
    snapshot = dest / "qdrant-full.snapshot"
    snapshot.write_bytes(b"snapshoT")
    assert bm.problems(dest, hashes=False) == [
        "rmp_db.dump is 12 bytes, the manifest says 11", "missing settings.json", "openclaw.json is empty"]
    assert bm.problems(dest, hashes=True) == [
        "rmp_db.dump is 12 bytes, the manifest says 11", "qdrant-full.snapshot does not match its checksum",
        "missing settings.json", "openclaw.json is empty"]
    # The run's own failures come first.
    snapshot.unlink()
    _write(dest, failures=["qdrant"])
    assert bm.problems(dest, hashes=True) == [
        "failed: qdrant, settings", "missing qdrant-full.snapshot", "missing settings.json", "openclaw.json is empty"]
    (dest / "manifest.json").unlink()
    assert bm.problems(dest, hashes=True) == ["no readable manifest.json"]


def test_problems_reports_a_backup_made_before_format_2_with_its_failures(dest):
    """20261005T031502Z's shape: no failures key and a 110-byte qdrant.tar.gz; and a later one with failures."""
    shutil.rmtree(dest)
    dest.mkdir()
    with tarfile.open(dest / "qdrant.tar.gz", "w:gz") as tar:
        info = tarfile.TarInfo("qdrant")
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
    (dest / "manifest.json").write_text(json.dumps({"timestamp": STAMP, "components": ["postgres", "qdrant"]}))
    assert bm.problems(dest, hashes=True, qdrant_mode="server") == ["made before manifest format 2 (no file list)"]
    (dest / "manifest.json").write_text(json.dumps({"timestamp": STAMP, "failures": ["qdrant", "temporal:temporal"]}))
    assert bm.problems(dest, hashes=False) == ["made before manifest format 2 (no file list)",
                                               "failed: qdrant, temporal:temporal"]


def test_problems_reports_an_embedded_qdrant_tar_while_qdrant_runs_as_a_server(dest):
    (dest / "qdrant-full.snapshot").unlink()
    with tarfile.open(dest / "qdrant.tar.gz", "w:gz") as tar:
        info = tarfile.TarInfo("qdrant")
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
    assert (dest / "qdrant.tar.gz").stat().st_size < 200
    manifest = _write(dest, qdrant_mode="embedded")
    assert manifest["failures"] == [] and manifest["components"]["qdrant"] == ["qdrant.tar.gz"]
    assert bm.problems(dest, hashes=True, qdrant_mode="server") == [
        "Qdrant runs as a server but this backup holds qdrant.tar.gz"]
    assert bm.problems(dest, hashes=True, qdrant_mode="embedded") == bm.problems(dest, hashes=True) == []


def test_required_components_are_exactly_the_seven_and_only_artifacts_may_be_empty(dest):
    assert bm.REQUIRED_COMPONENTS == ("postgres", "temporal", "qdrant", "artifacts", "openclaw-state", "workspace",
                                      "settings")
    _write(dest)
    manifest = json.loads((dest / "manifest.json").read_text())
    del manifest["components"]["workspace"]
    (dest / "manifest.json").write_text(json.dumps(manifest))
    assert bm.problems(dest, hashes=True) == ["no workspace"]
    # A run set to dump no Temporal database lists none, and fails.
    manifest = _write(dest, temporal_dbs=[])
    assert manifest["components"]["temporal"] == [] and manifest["failures"] == ["temporal"]
    assert bm.problems(dest, hashes=True) == ["failed: temporal", "no temporal"]
    # With the artifact store off the list is empty, and that is no problem.
    (dest / "artifacts.tar.gz").unlink()
    manifest = _write(dest, artifacts_enabled=False)
    assert manifest["components"]["artifacts"] == [] and manifest["failures"] == []
    assert bm.problems(dest, hashes=True) == []
    # With it on, the archive is listed even when it is missing.
    manifest = _write(dest)
    assert manifest["components"]["artifacts"] == ["artifacts.tar.gz"] and manifest["failures"] == ["artifacts"]
    assert bm.problems(dest, hashes=True) == ["failed: artifacts", "missing artifacts.tar.gz"]


def test_newest_is_the_newest_stamped_directory_that_holds_a_manifest(tmp_path):
    root = tmp_path / "backups"
    assert bm.newest(root) is None
    for name, manifest in (("20261019T031502Z", True), ("20261020T031502Z", True),
                           ("20261021T031502Z", False),            # a run in progress, or killed
                           ("openclaw-update-20261022T000000Z", True), ("openclaw-state", True)):
        (root / name).mkdir(parents=True)
        if manifest:
            (root / name / "manifest.json").write_text("{}")
    assert bm.newest(root) == root / "20261020T031502Z"


def test_the_command_line_writes_a_manifest_and_verifies_a_backup(dest, tmp_path, monkeypatch, capsys):
    fake, state = tmp_path / "bin", tmp_path / "state"
    fake.mkdir()
    state.mkdir()
    (fake / "pg_restore").write_text(FAKE_PG_RESTORE)
    (fake / "pg_restore").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_STATE", str(state))
    assert shutil.which("pg_restore") == str(fake / "pg_restore"), "pg_restore does not resolve to its fake"
    write = ["write", str(dest), "--temporal-dbs", "temporal temporal_visibility", "--qdrant-mode", "server",
             "--artifacts", "on"]
    assert bm.main(write) == 0
    assert capsys.readouterr().out == "\n"
    assert bm.main(["verify", str(dest)]) == 0
    assert capsys.readouterr().out == ""
    (dest / "openclaw.json").unlink()
    assert bm.main([*write, "--failure", "qdrant", "--failure", "temporal:temporal"]) == 0
    assert capsys.readouterr().out == "qdrant temporal:temporal settings\n"
    assert bm.main(["verify", str(dest)]) == 1
    assert capsys.readouterr().out.splitlines() == ["failed: qdrant, temporal:temporal, settings",
                                                    "missing openclaw.json"]
    (dest / "artifacts.tar.gz").unlink()
    assert bm.main([*write[:-1], "off"]) == 0
    assert capsys.readouterr().out == "settings\n"
    assert json.loads((dest / "manifest.json").read_text())["components"]["artifacts"] == []
