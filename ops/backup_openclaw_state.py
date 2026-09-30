"""Online backups of OpenClaw's SQLite stores, and their restore for a rollback.

The agent database (sessions and transcripts) and the state database (config state, auth
profiles, cron jobs) are copied with SQLite's backup API: a consistent snapshot, WAL included,
while the gateway keeps writing, where ``cp`` could catch a half-written page. Each copy is
checked with ``PRAGMA quick_check`` and described in ``manifest.json``.

- The upgrade takes one before anything stops.
- The nightly backup keeps one per night.
- A rollback restores one, with the gateway stopped.

    venv/bin/python ops/backup_openclaw_state.py backup [--dest DIR]
    venv/bin/python ops/backup_openclaw_state.py verify DIR
    venv/bin/python ops/backup_openclaw_state.py restore DIR --yes
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import OPENCLAW_HOME, RMP_DATA_DIR  # noqa: E402

DEFAULT_ROOT = Path(RMP_DATA_DIR) / "backups" / "openclaw-state"


def stores(home: Path = Path(OPENCLAW_HOME)) -> Dict[str, Path]:
    return {
        "agent": home / "agents" / "main" / "agent" / "openclaw-agent.sqlite",
        "state": home / "state" / "openclaw.sqlite",
    }


def _quick_check(path: Path) -> str:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return str(con.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        con.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _openclaw_version() -> str:
    try:
        run = subprocess.run(["openclaw", "--version"], capture_output=True, text=True, timeout=60)
        return run.stdout.strip().splitlines()[0] if run.returncode == 0 and run.stdout.strip() else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def backup(dest: Path, *, home: Path = Path(OPENCLAW_HOME), version: Optional[str] = None) -> dict:
    """Snapshot every store that exists into ``dest``; raises when a copy fails its check."""
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    files = {}
    for name, source in stores(home).items():
        if not source.is_file():
            continue
        target = dest / source.name
        partial = target.with_name(target.name + ".partial")
        partial.unlink(missing_ok=True)
        src = sqlite3.connect(source, timeout=30)
        dst = sqlite3.connect(partial)
        try:
            src.backup(dst)
            # One self-contained file: a WAL-mode copy would grow -wal/-shm files whenever it is read.
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
            src.close()
        check = _quick_check(partial)
        if check != "ok":
            raise RuntimeError(f"backup of {source} failed its check: {check}")
        os.replace(partial, target)
        os.chmod(target, 0o600)
        stat = source.stat()
        files[name] = {
            "source": str(source),
            "file": target.name,
            "bytes": target.stat().st_size,
            "sha256": _sha256(target),
            "quick_check": check,
            "mode": stat.st_mode & 0o7777,
            "uid": stat.st_uid,
            "gid": stat.st_gid,
        }
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "openclaw_version": version if version is not None else _openclaw_version(),
        "files": files,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify(src_dir: Path) -> dict:
    """The manifest, after checking every copy's checksum and integrity."""
    manifest = json.loads((src_dir / "manifest.json").read_text(encoding="utf-8"))
    for name, meta in manifest["files"].items():
        copy = src_dir / meta["file"]
        if _sha256(copy) != meta["sha256"]:
            raise RuntimeError(f"{copy} does not match its manifest checksum")
        check = _quick_check(copy)
        if check != "ok":
            raise RuntimeError(f"{copy} failed its check: {check}")
    return manifest


def gateway_active() -> bool:
    run = subprocess.run(["systemctl", "is-active", "--quiet", "openclaw-gateway"], capture_output=True)
    return run.returncode == 0


def restore(src_dir: Path, *, home: Path = Path(OPENCLAW_HOME), check_gateway: bool = True) -> dict:
    """Put a verified backup back in place. The gateway must be stopped."""
    if check_gateway and gateway_active():
        raise RuntimeError("stop openclaw-gateway before restoring its stores")
    manifest = verify(src_dir)
    targets = stores(home)
    for name, meta in manifest["files"].items():
        target = targets[name]
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(target.name + ".restoring")
        shutil.copyfile(src_dir / meta["file"], staged)
        os.chmod(staged, meta["mode"])
        os.chown(staged, meta["uid"], meta["gid"])
        # A WAL left from the replaced database would be replayed onto the restored one.
        for suffix in ("-wal", "-shm"):
            target.with_name(target.name + suffix).unlink(missing_ok=True)
        os.replace(staged, target)
    return manifest


def main(argv) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    take = sub.add_parser("backup", help="snapshot the stores")
    take.add_argument("--dest", type=Path, help="directory to write (default: a new timestamped one)")
    sub.add_parser("verify", help="check a backup").add_argument("dir", type=Path)
    back = sub.add_parser("restore", help="restore a backup (gateway stopped)")
    back.add_argument("dir", type=Path)
    back.add_argument("--yes", action="store_true", help="confirm replacing the live stores")
    args = parser.parse_args(argv)
    if args.action == "backup":
        dest = args.dest or DEFAULT_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        manifest = backup(dest)
        sizes = ", ".join(f"{name} {meta['bytes'] // (1 << 20)} MiB" for name, meta in manifest["files"].items())
        print(f"openclaw state backup: {dest} ({sizes}; {manifest['openclaw_version'] or 'version unknown'})")
    elif args.action == "verify":
        manifest = verify(args.dir)
        print(f"ok: {args.dir} ({', '.join(manifest['files'])}, taken {manifest['created_at']})")
    else:
        if not args.yes:
            print("refusing without --yes: this replaces the live OpenClaw stores", file=sys.stderr)
            return 2
        manifest = restore(args.dir)
        print(f"restored {', '.join(manifest['files'])} from {args.dir} (taken {manifest['created_at']}, "
              f"{manifest['openclaw_version'] or 'version unknown'})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
