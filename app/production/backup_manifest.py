"""The manifest of a backup made by ops/backup.sh, format 2: every component's files with their sizes and sha256.

One definition of a complete backup, for the backup that writes the manifest and for everything that reads one: the
restore (ops/restore_backup.sh) and the restore drill (ops/restore_drill.py) verify a backup with its checksums before
they use it.

    python -m app.production.backup_manifest write <dir> --temporal-dbs "<names>" --qdrant-mode <mode>
        --artifacts on|off [--failure <name>]...
    python -m app.production.backup_manifest offsite <dir> off|copied|failed
    python -m app.production.backup_manifest verify <dir>

``write`` runs from the RMP checkout once every component has run. It prints the run's final failures on stdout,
space-separated, and the reason for each failure it adds on stderr. ``offsite`` then records what became of the
backup's off-host copy (ops/offsite_copy.sh). ``verify`` prints one problem per line and exits 1 when there are any.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
import zlib
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

FORMAT = 2
MANIFEST = "manifest.json"
# The components of every format-2 manifest, in this order. Each lists one file or more, except artifacts while the
# artifact store is disabled.
REQUIRED_COMPONENTS = ("postgres", "temporal", "qdrant", "artifacts", "openclaw-state", "workspace", "settings")
# A backup directory is named after the UTC time its run started.
STAMP = re.compile(r"20\d{6}T\d{6}Z")
# The file a run makes of Qdrant, by the mode Qdrant runs in.
QDRANT_FILES = {"server": "qdrant-full.snapshot", "embedded": "qdrant.tar.gz"}
OPENCLAW_STATE = "openclaw-state"
# The stores ops/backup_openclaw_state.py copies; its manifest must list both.
OPENCLAW_STORES = ("agent", "state")
# The files read back before the manifest is written: pg_dump's archives and the tar archives.
READ_BACK = (".dump", ".tar.gz")
LEGACY = "made before manifest format 2 (no file list)"
# What became of the backup's off-host copy: not a component, a field of its own. "off" while settings'
# backup.offsite.enabled is not true, an accepted exception (K-7). A failed copy is also the run failure "offsite".
OFFSITE = "offsite"
OFFSITE_STATES = ("off", "copied", "failed")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_back(path: Path) -> bool:
    """Whether a dump or an archive reads to its end. pg_restore reads every block of a dump and writes its SQL
    nowhere; tarfile reads every member of an archive, and gzip then checks the stream's checksum. pg_restore's own
    message goes to stderr."""
    try:
        if path.name.endswith(".dump"):
            return subprocess.run(["pg_restore", "-f", os.devnull, str(path)], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL).returncode == 0
        with tarfile.open(path, "r:gz") as tar:
            for _ in tar:
                pass
            while tar.fileobj.read(1 << 20):
                pass
        return True
    except (OSError, EOFError, tarfile.TarError, zlib.error):
        return False


def offsite_config() -> dict:
    """Whether ops/backup.sh copies each backup off the host with restic: settings' backup.offsite.enabled, false
    unless set. It stays unset on this host, an accepted exception (K-7). app.config is imported here only: writing,
    recording and verifying a manifest need nothing else of RMP's."""
    from app.config import load_settings

    offsite = (load_settings().get("backup") or {}).get("offsite") or {}
    return {"enabled": bool(offsite.get("enabled", False))}


def _named(failures: Sequence[str], name: str) -> bool:
    """Whether ``failures`` already holds ``name``, itself or one of its parts (``temporal:<db>``)."""
    return any(entry == name or entry.startswith(f"{name}:") for entry in failures)


def _openclaw_state(dest: Path) -> Tuple[List[str], Optional[str]]:
    """The OpenClaw store copies' files: their manifest and every file it lists. With them, why the copies are
    incomplete, if they are."""
    listed = [f"{OPENCLAW_STATE}/{MANIFEST}"]
    try:
        stores = json.loads((dest / OPENCLAW_STATE / MANIFEST).read_text(encoding="utf-8"))["files"]
        listed += [f"{OPENCLAW_STATE}/{meta['file']}" for meta in stores.values()]
    except FileNotFoundError:
        return listed, None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return listed, f"{OPENCLAW_STATE}/{MANIFEST} does not read"
    absent = [store for store in OPENCLAW_STORES if store not in stores]
    return listed, f"{OPENCLAW_STATE}/{MANIFEST} does not list {', '.join(absent)}" if absent else None


def write(dest: Path, *, failures: Sequence[str], temporal_dbs: Sequence[str], qdrant_mode: str,
          artifacts_enabled: bool, read_back: Callable[[Path], bool] = read_back) -> dict:
    """Write ``dest/manifest.json`` and return it.

    ``failures`` are the components the run already failed, each kept once. A component is added to them, once, when
    it has no file to list, or when an expected file is missing, empty or unreadable. A dump or an archive that does
    not read back adds its own name: ``postgres``, ``temporal:<db>``, ``artifacts`` or ``workspace``. Every expected
    file is listed even when it is missing, and every one that exists gets its size and sha256.
    """
    dest = Path(dest)
    failures = list(dict.fromkeys(failures))

    def fail(name: str, reason: str) -> None:
        if not _named(failures, name):
            failures.append(name)
            print(f"FAIL: {name}: {reason}", file=sys.stderr, flush=True)

    qdrant = QDRANT_FILES.get(qdrant_mode)
    openclaw, openclaw_incomplete = _openclaw_state(dest)
    components: Dict[str, List[str]] = {
        "postgres": ["rmp_db.dump"],
        "temporal": [f"{db}.dump" for db in temporal_dbs],
        "qdrant": [qdrant] if qdrant else [],
        "artifacts": ["artifacts.tar.gz"] if artifacts_enabled else [],
        OPENCLAW_STATE: openclaw,
        "workspace": ["workspace.tar.gz"],
        "settings": ["settings.json", "openclaw.json"],
    }
    incomplete = {"temporal": "the run dumped no Temporal database",
                  "qdrant": f"no backup file for Qdrant's mode {qdrant_mode!r}"}
    files: Dict[str, dict] = {}
    for component, names in components.items():
        if not names and component != "artifacts":
            fail(component, incomplete[component])
        if component == OPENCLAW_STATE and openclaw_incomplete:
            fail(component, openclaw_incomplete)
        for name in names:
            path = dest / name
            if not path.is_file():
                fail(component, f"{name} is missing")
                continue
            try:
                files[name] = {"bytes": path.stat().st_size, "sha256": _sha256(path)}
            except OSError as exc:
                fail(component, f"{name} does not read ({exc.strerror or type(exc).__name__})")
                continue
            if files[name]["bytes"] == 0:
                fail(component, f"{name} is empty")
            elif name.endswith(READ_BACK) and not read_back(path):
                part = f"temporal:{name[:-len('.dump')]}" if component == "temporal" else component
                fail(part, f"{name} does not read back")
    manifest = {"format": FORMAT, "timestamp": dest.name, "host": socket.gethostname(), "qdrant_mode": qdrant_mode,
                "components": components, "files": files, "failures": failures}
    _save(dest, manifest)
    return manifest


def record_offsite(dest: Path, state: str) -> dict:
    """Record in ``dest/manifest.json`` what became of the backup's off-host copy, ``off``, ``copied`` or ``failed``,
    and return the manifest. A failed copy also fails the run: ``offsite`` joins its failures, once."""
    if state not in OFFSITE_STATES:
        raise ValueError(f"the off-host copy is {' or '.join(OFFSITE_STATES)}, not {state!r}")
    dest = Path(dest)
    manifest = json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
    failures = manifest.pop("failures")
    if state == "failed" and OFFSITE not in failures:
        failures.append(OFFSITE)
    manifest.pop(OFFSITE, None)
    manifest.update({OFFSITE: state, "failures": failures})
    _save(dest, manifest)
    return manifest


def _save(dest: Path, manifest: dict) -> None:
    """Whole or not at all: a reader never sees half a manifest, and the backup's exit removes a partial one."""
    partial = dest / f"{MANIFEST}.partial"
    partial.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, dest / MANIFEST)


def _file_problems(dest: Path, name: str, meta: object, hashes: bool) -> List[str]:
    path = dest / name
    if not path.is_file():
        return [f"missing {name}"]
    size = path.stat().st_size
    if size == 0:
        return [f"{name} is empty"]
    # write() gives every file it lists a size unless the file was missing or unreadable, and then fails its component.
    # A file here without one has nothing to be checked against.
    if not isinstance(meta, dict):
        return [f"{name} has no size in the manifest"]
    if size != meta.get("bytes"):
        return [f"{name} is {size} bytes, the manifest says {meta.get('bytes')}"]
    if hashes:
        try:
            matches = _sha256(path) == meta.get("sha256")
        except OSError:
            matches = False
        if not matches:
            return [f"{name} does not match its checksum"]
    return []


def problems(dest: Path, *, hashes: bool, qdrant_mode: Optional[str] = None) -> List[str]:
    """What keeps the backup in ``dest`` from being complete, one line each; empty when it is. Every listed file's size
    is checked, its checksum only with ``hashes``, which reads every file. ``qdrant_mode`` is the mode Qdrant runs in
    now, when the caller knows it."""
    dest = Path(dest)
    try:
        manifest = json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = None
    if not isinstance(manifest, dict):
        return [f"no readable {MANIFEST}"]
    recorded = manifest.get("failures")
    failed = [f"failed: {', '.join(map(str, recorded))}"] if isinstance(recorded, list) and recorded else []
    if manifest.get("format") != FORMAT:
        return [LEGACY, *failed]
    found = list(failed)
    components = manifest.get("components") if isinstance(manifest.get("components"), dict) else {}
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    for component in REQUIRED_COMPONENTS:
        names = components.get(component)
        # An empty artifacts list is what a run with the artifact store disabled writes.
        if not isinstance(names, list) or not (names or component == "artifacts"):
            found.append(f"no {component}")
    for names in components.values():
        for name in names if isinstance(names, list) else []:
            found += _file_problems(dest, str(name), files.get(name), hashes)
    qdrant = components.get("qdrant")
    if qdrant_mode == "server" and isinstance(qdrant, list) and qdrant and qdrant != [QDRANT_FILES["server"]]:
        found.append(f"Qdrant runs as a server but this backup holds {', '.join(map(str, qdrant))}")
    return found


def newest(root: Path) -> Optional[Path]:
    """The newest backup under ``root``: the newest directory named like a stamp that holds a manifest.json. A run in
    progress has none yet, and a killed run never writes one. Other directories there, such as openclaw-state/ and an
    OpenClaw update's backup, are never one. Its age comes from its stamp."""
    root = Path(root)
    if not root.is_dir():
        return None
    backups = [p for p in root.iterdir() if STAMP.fullmatch(p.name) and (p / MANIFEST).is_file()]
    return max(backups, key=lambda p: p.name, default=None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.production.backup_manifest",
                                     description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    make = sub.add_parser("write", help="write a backup's manifest and print its failures")
    make.add_argument("dir", type=Path)
    make.add_argument("--temporal-dbs", required=True, help="the Temporal databases the run dumped, space-separated")
    make.add_argument("--qdrant-mode", required=True, help="the mode Qdrant ran in: server or embedded")
    make.add_argument("--artifacts", required=True, choices=("on", "off"), help="whether the artifact store is on")
    make.add_argument("--failure", action="append", default=[], help="a component the run failed (repeatable)")
    offsite = sub.add_parser("offsite", help="record what became of the backup's off-host copy")
    offsite.add_argument("dir", type=Path)
    offsite.add_argument("state", choices=OFFSITE_STATES)
    sub.add_parser("verify", help="check a backup against its manifest, checksums included").add_argument(
        "dir", type=Path)
    args = parser.parse_args(argv)
    if args.action == "write":
        manifest = write(args.dir, failures=args.failure, temporal_dbs=args.temporal_dbs.split(),
                         qdrant_mode=args.qdrant_mode, artifacts_enabled=args.artifacts == "on")
        print(" ".join(manifest["failures"]))
        return 0
    if args.action == "offsite":
        record_offsite(args.dir, args.state)
        return 0
    found = problems(args.dir, hashes=True)
    for problem in found:
        print(problem)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
