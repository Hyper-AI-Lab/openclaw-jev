"""ops/backup.sh end to end against fake pg_dump and a fake Qdrant API: a real snapshot, loud failures, safe pruning."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "ops" / "backup.sh"

FAKE_CURL = r'''#!/usr/bin/env python3
"""curl, as far as backup.sh uses it against Qdrant's snapshot API."""
import io, json, os, sys, tarfile
args, method, out, url, i = sys.argv[1:], "GET", None, None, 0
while i < len(args):
    a = args[i]
    if a == "-X": method = args[i + 1]; i += 2; continue
    if a in ("-o", "-m"):
        if a == "-o": out = args[i + 1]
        i += 2; continue
    if a.startswith("-"): i += 1; continue
    url = a; i += 1
with open(os.environ["FAKE_CURL_LOG"], "a") as log:
    log.write(f"{method} {url}\n")
if os.environ.get("FAKE_QDRANT_FAIL") == "1":
    sys.exit(22)
if method == "POST" and url.endswith("/snapshots?wait=true"):
    print(json.dumps({"result": {"name": "full-snapshot-test.snapshot"}, "status": "ok"}))
elif method == "GET" and "/snapshots/" in url:
    with tarfile.open(out, "w") as tar:
        data = b"segment"
        info = tarfile.TarInfo("collections/rmp_memories/0/segment")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
elif method == "DELETE":
    print('{"result": true}')
else:
    sys.exit(22)
'''

FAKE_PG_DUMP = r'''#!/bin/sh
[ "${FAKE_PGDUMP_FAIL:-0}" = 1 ] && { echo "pg_dump: connection refused" >&2; exit 1; }
while [ $# -gt 0 ]; do [ "$1" = "-f" ] && { printf 'PGDMP' > "$2"; exit 0; }; shift; done
exit 1
'''


@pytest.fixture
def backup(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    for name, body in (("curl", FAKE_CURL), ("pg_dump", FAKE_PG_DUMP)):
        (fake / name).write_text(body)
        (fake / name).chmod(0o755)
    root = tmp_path / "backups"
    root.mkdir()
    curl_log = tmp_path / "curl.log"
    env = {**os.environ, "RMP_ROOT": str(REPO), "RMP_BACKUP_ROOT": str(root), "RMP_PYTHON": sys.executable,
           "PATH": f"{fake}:{os.environ['PATH']}", "FAKE_CURL_LOG": str(curl_log)}

    def run(**extra):
        result = subprocess.run(["bash", str(SCRIPT)], env={**env, **extra}, capture_output=True, text=True, timeout=300)
        stamps = sorted(p for p in root.iterdir() if p.name[:2] == "20")
        dest = stamps[-1] if stamps else None
        manifest = json.loads((dest / "manifest.json").read_text()) if dest else None
        calls = curl_log.read_text().splitlines() if curl_log.exists() else []
        return result, dest, manifest, calls

    run.root = root
    return run


def test_a_healthy_backup_takes_a_real_qdrant_snapshot_and_reports_no_failures(backup):
    result, dest, manifest, calls = backup()
    assert result.returncode == 0, result.stdout + result.stderr
    assert manifest["failures"] == []
    assert (dest / "rmp_db.dump").read_bytes() == b"PGDMP"
    assert (dest / "qdrant-full.snapshot").stat().st_size > 0
    assert not (dest / "qdrant.tar.gz").exists()
    assert [c.split()[0] for c in calls] == ["POST", "GET", "DELETE"]
    assert calls[0].endswith("/snapshots?wait=true") and calls[1].endswith("/snapshots/full-snapshot-test.snapshot")


def test_a_failed_qdrant_snapshot_fails_the_run_but_every_other_component_still_runs(backup):
    result, dest, manifest, _ = backup(FAKE_QDRANT_FAIL="1")
    assert result.returncode == 1
    assert manifest["failures"] == ["qdrant"]
    assert (dest / "rmp_db.dump").exists() and (dest / "openclaw-state").exists()
    assert "Backup INCOMPLETE: qdrant failed" in result.stdout


def test_a_failed_postgres_dump_is_reported_not_swallowed(backup):
    result, dest, manifest, _ = backup(FAKE_PGDUMP_FAIL="1")
    assert result.returncode == 1 and manifest["failures"] == ["postgres"]
    assert (dest / "qdrant-full.snapshot").exists()


def test_pruning_keeps_fourteen_dated_backups_and_never_touches_other_directories(backup):
    old = time.time() - 30 * 86400
    keep = backup.root / "openclaw-update-20260930T222604Z"
    keep.mkdir()
    os.utime(keep, (old - 86400, old - 86400))
    for day in range(1, 17):
        stamp = backup.root / f"202609{day:02d}T031500Z"
        stamp.mkdir()
        os.utime(stamp, (old + day * 3600, old + day * 3600))
    result, _, manifest, _ = backup()
    assert result.returncode == 0, result.stdout + result.stderr
    dated = sorted(p.name for p in backup.root.iterdir() if p.name[:2] == "20")
    assert len(dated) == 14 and "20260903T031500Z" not in dated and "20260904T031500Z" in dated  # 16 old + 1 new: the 3 oldest go
    assert keep.is_dir(), "a non-dated directory must never be pruned by age"
