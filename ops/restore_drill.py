#!/usr/bin/env python3
"""The restore drill: a backup restored into a scratch PostgreSQL and a scratch Qdrant, checked, and removed.

Run it as root from the live checkout, on an idle host and outside the backup's window (03:00-03:45 UTC):

    cd /root/.openclaw/rmp && venv/bin/python ops/restore_drill.py [<backup dir>]

It drills the newest stamped backup under data/backups whose manifest has "failures": [], unless given another.

- restic: the backup goes through a scratch restic repository and back. ops/offsite_copy.sh, the off-host step
  ops/backup.sh runs, copies it into a repository in the drill's scratch directory under /var/tmp, whose password is
  random and lives only in a 0600 file there, and restic restores it there. The restored copy must match the backup's
  manifest, checksums included, and every leg after this one reads it, so the drill proves a restore from the off-host
  copy. The drill never reads /etc/rmp/restic.env and drops every RESTIC_ variable of its own environment. A missing
  restic, or a round trip that fails, is a FAIL; the other legs then read the backup itself.
- PostgreSQL: a throwaway server from this host's own binaries, the server that made the dumps. initdb and pg_ctl run
  as postgres, with the data and the socket in a scratch directory under /var/tmp, port 55432 and no TCP listener.
  Production's roles are created there NOLOGIN, so the archives' OWNER TO lines apply; their names come from the
  drill's one production read, a read-only query. Each dump is restored through ops/restore_pg_database.sh, the
  restore's own command.
- Qdrant: a container named rmp-qdrant-drill, of the image ops/start_qdrant.sh runs, on 127.0.0.1:6937 with 1 GiB and
  no volume. It requires a key made for this drill, which stays in this process's memory: docker and
  ops/restore_qdrant_snapshot.sh get it in their environment and curl on stdin, never on a command line, and nothing
  prints it. 6937 is in coding.blocked_tcp_ports, so aura-coder cannot reach the copy of Aura's memory.

The checks: first the backup against its manifest, with every file's size and checksum, before anything is created; a
backup that does not verify is not drilled. Then the restic round trip, each database's owner, rmp_db's vector
extension, every table's row count against its COPY block in the dump, the collections against the snapshot, the
OpenClaw store copies, and every archive read to its end. Each prints one line, ok or FAIL, with names and counts only. The last line is the restore
duration, from the first database restore to the end of the Qdrant restore: the steps a real restore repeats on the
same data.

It exits 0 when every check passed and 1 when one failed. It exits 2, having created nothing, when the host is not
ready for it. The scratch instances and the scratch directory are removed however the drill ends, after a SIGTERM,
SIGINT or SIGHUP too; started under nohup, which ignores SIGHUP, it keeps running when its session hangs up. After a
hard kill, remove them with:

    docker rm -f rmp-qdrant-drill; for d in /var/tmp/rmp-restore-drill-*; do sudo -n -u postgres /usr/lib/postgresql/16/bin/pg_ctl -D "$d/pg/data" stop -m fast; rm -rf "$d"; done
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.coding import firewall  # noqa: E402
from app.config import RMP_DATA_DIR, get_coding_config  # noqa: E402
from app.production import backup_manifest  # noqa: E402
from ops import backup_openclaw_state  # noqa: E402

# The server that made the dumps; RMP_PG_BIN points the tests elsewhere.
PG_BIN = "/usr/lib/postgresql/16/bin"
PG_PORT = 55432
# Inside coding.blocked_tcp_ports' 6933-6939 and used by nothing; Temporal holds 6933-6935 and 6939.
DRILL_QDRANT_PORT = 6937
DRILL_CONTAINER = "rmp-qdrant-drill"
QDRANT_URL = f"http://127.0.0.1:{DRILL_QDRANT_PORT}"
KEY_VARIABLE = "QDRANT__SERVICE__API_KEY"
SCRATCH_ROOT = Path("/var/tmp")
SCRATCH_PREFIX = "rmp-restore-drill-"
# Where restic restores the drilled backup, in the scratch directory; the backup lies under it at its own path.
FROM_RESTIC = "from-restic"
# Qdrant's 1 GiB, the scratch server and the drill's own reads.
MIN_AVAILABLE = 2 << 30
# The coding units: none may run during a drill, so no deploy's post-check falls inside one.
CODING_UNITS = ("aura-claude-*", "aura-verify-*", "aura-collect-*", "aura-direct-*", "aura-deploy-*")
# Each database a backup dumps, with the owner its archive gives it.
OWNERS = {"rmp_db": "rmp", "temporal": "temporal", "temporal_visibility": "temporal"}
STAMP = re.compile(r"20\d{6}T\d{6}Z")
# The drill's one read of production's server: the roles the archives may name. The SET is its own -c, so it ends
# its own transaction and the SELECT's starts read-only; in one -c string both share a transaction that is not.
ROLE_QUERY = ("SET default_transaction_read_only=on",
              "SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_' AND rolname <> 'postgres'")
# Every ordinary table outside the system schemas, named as pg_dump names it, with the rows it holds itself.
COUNT_QUERY = ("SELECT format('%I.%I', n.nspname, c.relname), "
               "(xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM ONLY %I.%I', n.nspname, "
               "c.relname), false, true, '')))[1]::text "
               "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
               "WHERE c.relkind = 'r' AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'")
# A collection's snapshot in a full snapshot, <collection>-<peer id>-<date>.snapshot, as
# ops/restore_qdrant_snapshot.sh reads it.
SNAPSHOT_SUFFIX = re.compile(r"-[0-9]+-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}\.snapshot$")
_NAME = rb'(?:"(?:[^"]|"")*"|[^\s".(]+)'
# The header of a table's data in pg_restore's SQL: COPY <schema>.<table> (<columns>) FROM stdin;
COPY_HEADER = re.compile(rb"COPY (" + _NAME + rb"\." + _NAME + rb")(?: \(.*\))? FROM stdin;")
HARD_KILL_CLEANUP = (f"docker rm -f {DRILL_CONTAINER}; for d in {SCRATCH_ROOT}/{SCRATCH_PREFIX}*; do "
                     f'sudo -n -u postgres {PG_BIN}/pg_ctl -D "$d/pg/data" stop -m fast; rm -rf "$d"; done')
SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)

# Every external command: its argv, with an environment (None: this process's) and stdin. Output comes back as bytes.
Run = Callable[..., "subprocess.CompletedProcess[bytes]"]


def _run(argv: Sequence[str], *, env: Optional[Dict[str, str]] = None,
         input: Optional[bytes] = None) -> "subprocess.CompletedProcess[bytes]":
    """Every command's output is captured: the drill prints names and counts only."""
    stdin = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(list(argv), env=env, capture_output=True, cwd="/", **stdin)


def _mem_available() -> int:
    with open("/proc/meminfo", encoding="ascii") as meminfo:
        for line in meminfo:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    raise ValueError("/proc/meminfo has no MemAvailable")


def _active_user_tasks() -> int:
    from app.production.canary_sentinel import count_active_user_tasks_sync

    return count_active_user_tasks_sync(strict=True)


def _port_taken(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


@dataclass
class Probes:
    """What the preconditions read from this host besides systemctl and docker; the tests inject their own."""
    euid: Callable[[], int] = os.geteuid
    mem_available: Callable[[], int] = _mem_available
    active_user_tasks: Callable[[], int] = _active_user_tasks
    port_taken: Callable[[int], bool] = _port_taken


def newest_clean(root: Path) -> Optional[Path]:
    """The newest backup under ``root`` whose manifest has "failures": []. A run in progress or killed has no
    manifest, and a run from before the failures list has no such key."""
    if not root.is_dir():
        return None
    for path in sorted((p for p in root.iterdir() if STAMP.fullmatch(p.name) and p.is_dir()), reverse=True):
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(manifest, dict) and manifest.get("failures") == []:
            return path
    return None


def qdrant_image(script: Path = REPO / "ops" / "start_qdrant.sh") -> Optional[str]:
    """The image rmp-qdrant runs, the server that made the snapshots; None unless the start script names exactly
    one."""
    images = set(re.findall(r"qdrant/qdrant:[\w.-]+", script.read_text(encoding="utf-8")))
    return images.pop() if len(images) == 1 else None


def refusals(run: Run, probes: Probes) -> List[str]:
    """Why the drill may not start now, one reason per failed precondition; empty when it may. Nothing here creates
    anything."""
    reasons = []
    if probes.euid() != 0:
        reasons.append("the drill runs as root")
    try:
        available = probes.mem_available()
    except (OSError, ValueError) as exc:
        reasons.append(f"could not read MemAvailable ({type(exc).__name__})")
    else:
        if available < MIN_AVAILABLE:
            reasons.append(f"MemAvailable is {available >> 20} MiB; the drill needs {MIN_AVAILABLE >> 20} MiB")
    try:
        busy = probes.active_user_tasks()
    except Exception as exc:  # an unknown count is busy
        reasons.append(f"could not count the active user tasks ({type(exc).__name__}); that counts as busy")
    else:
        if busy:
            reasons.append(f"{busy} user task(s) active; run the drill when Aura is idle")
    listed = run(["systemctl", "list-units", "--plain", "--no-legend", "--state=active,activating", *CODING_UNITS])
    if listed.returncode != 0:
        reasons.append("systemctl could not list the coding units")
    else:
        units = [line.split()[0] for line in listed.stdout.decode(errors="replace").splitlines() if line.strip()]
        if units:
            reasons.append(f"{', '.join(units)} active; run the drill when no coding unit runs")
    try:
        covered = firewall.covers(firewall.port_elements(get_coding_config()["blocked_tcp_ports"]), DRILL_QDRANT_PORT)
    except ValueError:
        covered = False
    if not covered:
        reasons.append(f"port {DRILL_QDRANT_PORT} is not in coding.blocked_tcp_ports, so aura-coder could reach the "
                       "drill's Qdrant")
    if probes.port_taken(DRILL_QDRANT_PORT):
        reasons.append(f"something accepts connections on 127.0.0.1:{DRILL_QDRANT_PORT}")
    listed = run(["docker", "ps", "--all", "--filter", f"name={DRILL_CONTAINER}", "--format", "{{.Names}}"])
    if listed.returncode != 0:
        reasons.append("docker could not list the containers")
    elif DRILL_CONTAINER in listed.stdout.decode(errors="replace").split():
        reasons.append(f"a container named {DRILL_CONTAINER} exists; an earlier drill was killed: {HARD_KILL_CLEANUP}")
    return reasons


def copy_rows(lines: Iterable[bytes]) -> Dict[str, int]:
    """The rows of each COPY block in pg_restore's SQL, by table. One line is one row: COPY's text format escapes the
    newlines in a value."""
    rows: Dict[str, int] = {}
    table = None
    for line in lines:
        line = line[:-1] if line.endswith(b"\n") else line
        if table is None:
            header = COPY_HEADER.fullmatch(line)
            if header:
                table = header.group(1).decode(errors="replace")
                rows.setdefault(table, 0)
        elif line == b"\\.":
            table = None
        else:
            rows[table] += 1
    if table is not None:
        raise ValueError(f"the COPY block of {table} has no end")
    return rows


def row_mismatches(counted: Dict[str, int], copied: Dict[str, int]) -> List[str]:
    """Each table whose count(*) differs from the rows of its COPY block, or that only one side has."""
    mismatches = []
    for table in sorted(counted.keys() | copied.keys()):
        if table not in copied:
            mismatches.append(f"{table} has no COPY block")
        elif table not in counted:
            mismatches.append(f"{table} is not in the restored database")
        elif counted[table] != copied[table]:
            mismatches.append(f"{table} has {counted[table]} rows, its COPY block {copied[table]}")
    return mismatches


def snapshot_collections(snapshot: Path) -> Set[str]:
    """The collections a full snapshot holds, named as ops/restore_qdrant_snapshot.sh names them."""
    with tarfile.open(snapshot) as tar:
        names = [member.name.removeprefix("./") for member in tar.getmembers() if member.isfile()]
    return {SNAPSHOT_SUFFIX.sub("", name) for name in names if "/" not in name and SNAPSHOT_SUFFIX.search(name)} - {""}


def archive_entries(archive: Path) -> int:
    """The entries of a .tar.gz, read to the end of its gzip stream, whose checksum gzip then checks."""
    with tarfile.open(archive, "r:gz") as tar:
        entries = len(tar.getmembers())
        while tar.fileobj.read(1 << 20):
            pass
    return entries


def _hold_signals() -> None:
    """From here on SIGTERM, SIGINT and SIGHUP are ignored: nothing may cut the cleanup short."""
    for sig in SIGNALS:
        signal.signal(sig, signal.SIG_IGN)


def _exit_on_signal(signum, frame) -> None:
    _hold_signals()
    raise SystemExit(128 + signum)


class Drill:
    """One drill of ``backup``: its scratch instances, the restores, the checks and their lines."""

    def __init__(self, backup: Path, *, run: Run, scratch_root: Path, pg_bin: str, image: str):
        self.backup, self.run, self.scratch_root, self.pg_bin, self.image = backup, run, scratch_root, pg_bin, image
        # Held here only: docker and the snapshot restore get it in their environment, curl on stdin.
        self.key = secrets.token_hex(32)
        self.scratch: Optional[Path] = None
        self.pg_started = False
        self.container_created = False
        self.failed = False

    @property
    def snapshot(self) -> Path:
        return self.backup / "qdrant-full.snapshot"

    @property
    def pg(self) -> Path:
        """The scratch server's directory, owned by postgres: its data, its log and its socket."""
        return self.scratch / "pg"

    def ok(self, text: str) -> None:
        print(f"ok {text}", flush=True)

    def fail(self, text: str) -> None:
        self.failed = True
        print(f"FAIL {text}", flush=True)

    def call(self, check: str, step: str, argv: Sequence[str], **kwargs) -> Optional["subprocess.CompletedProcess"]:
        """The command's result, or None after a FAIL line for ``check`` when it exited non-zero."""
        result = self.run(argv, **kwargs)
        if result.returncode != 0:
            self.fail(f"{check}: {step} exited {result.returncode}")
            return None
        return result

    def as_postgres(self, tool: str, *args: str) -> List[str]:
        return ["sudo", "-n", "-u", "postgres", f"{self.pg_bin}/{tool}", *args]

    def psql(self, database: str, sql: str) -> List[str]:
        """psql on the scratch server's socket."""
        return self.as_postgres("psql", "-h", str(self.pg), "-p", str(PG_PORT), "-d", database, "-At", "-q", "-X",
                                "-c", sql)

    def perform(self) -> int:
        """The restic round trip, both scratch instances, the restores, the checks; then the cleanup and the restore
        duration. 0 when every check passed."""
        try:
            self.scratch = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=self.scratch_root))
            os.chmod(self.scratch, 0o711)
            self.restic_round_trip()
            postgres = self.start_postgres()
            qdrant = self.start_qdrant()
            started = time.monotonic()
            restored = self.restore_databases() if postgres else []
            collections = qdrant and self.restore_qdrant()
            duration = round(time.monotonic() - started)
            for db in restored:
                self.check_database(db)
            if collections:
                self.check_collections()
            self.check_openclaw()
            self.check_archives()
        finally:
            # A signal whose handler runs before all three are ignored still ends in the cleanup.
            try:
                _hold_signals()
            finally:
                self.cleanup()
        print(f"restore duration: {duration // 60} min {duration % 60} s", flush=True)
        return 1 if self.failed else 0

    def restic_round_trip(self) -> None:
        """The backup through a scratch restic repository and back: ops/offsite_copy.sh, the step ops/backup.sh runs,
        copies it in, restic restores it, and the restored copy must match its manifest, checksums included. Every leg
        after this one then reads the restored copy; after a FAIL line, the backup itself."""
        check = "restic round trip"
        work = self.scratch / "restic"
        work.mkdir(mode=0o700)
        password = work / "password"
        # The repository's password exists only in this file, which restic reads; nothing prints it.
        with os.fdopen(os.open(password, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
            file.write(secrets.token_hex(32))
        # No RESTIC_ variable of the caller's environment comes along: none may point restic at another repository or
        # password. TMPDIR: restic's temporary files go in the scratch directory, which the cleanup removes.
        env = {name: value for name, value in os.environ.items() if not name.startswith("RESTIC_")}
        env.update(RESTIC_REPOSITORY=str(work / "repo"), RESTIC_PASSWORD_FILE=str(password),
                   RESTIC_CACHE_DIR=str(work / "cache"), TMPDIR=str(work))
        target = self.scratch / FROM_RESTIC
        try:
            initialised = self.call(check, "restic init", ["restic", "init"], env=env)
        except FileNotFoundError:
            self.fail(f"{check}: restic is not installed")
            return
        if not (initialised
                and self.call(check, "offsite_copy.sh",
                              ["bash", str(REPO / "ops" / "offsite_copy.sh"), str(self.backup)], env=env)
                and self.call(check, "restic restore",
                              ["restic", "restore", "latest", "--target", str(target)], env=env)):
            return
        restored = target / self.backup.relative_to(self.backup.anchor)
        problems = backup_manifest.problems(restored, hashes=True)
        for problem in problems:
            self.fail(f"{check}: {problem}")
        if problems:
            return
        files = json.loads((restored / backup_manifest.MANIFEST).read_text(encoding="utf-8"))["files"]
        self.ok(f"{check}: {len(files)} files, checksums match")
        self.backup = restored

    def start_postgres(self) -> bool:
        """The scratch server, running with production's roles; False after a FAIL line."""
        check, data = "scratch postgres", str(self.pg / "data")
        self.pg.mkdir(mode=0o700)
        if not (self.call(check, "chown", ["chown", "postgres:postgres", str(self.pg)])
                and self.call(check, "initdb", self.as_postgres("initdb", "-D", data, "--auth=peer",
                                                                "--username=postgres", "--encoding=UTF8"))):
            return False
        # Recorded first: a start that fails or is cut short can leave a server running.
        self.pg_started = True
        options = f"-c listen_addresses='' -c port={PG_PORT} -c unix_socket_directories={self.pg}"
        start = self.as_postgres("pg_ctl", "-D", data, "-l", str(self.pg / "server.log"), "-w", "-o", options, "start")
        if not self.call(check, "pg_ctl start", start):
            return False
        # -q: psql would print SET's status line before the names.
        listed = self.call("production roles", "psql", ["sudo", "-n", "-u", "postgres", f"{self.pg_bin}/psql",
                                                        "-d", "postgres", "-At", "-q", "-X",
                                                        "-c", ROLE_QUERY[0], "-c", ROLE_QUERY[1]])
        if listed is None:
            return False
        roles = [line for line in listed.stdout.decode(errors="replace").splitlines() if line]
        create = "; ".join('CREATE ROLE "{}" NOLOGIN'.format(role.replace('"', '""')) for role in roles)
        if roles and not self.call(check, "CREATE ROLE", self.psql("postgres", create)):
            return False
        self.ok(f"{check}: port {PG_PORT}, socket only, production's roles {', '.join(roles) or 'none'} NOLOGIN")
        return True

    def start_qdrant(self) -> bool:
        """The scratch Qdrant, keyed; False after a FAIL line."""
        check = "scratch qdrant"
        if not self.snapshot.is_file():
            self.fail(f"{check}: the backup holds no {self.snapshot.name}")
            return False
        # Recorded first: a run that fails or is cut short can leave the container behind.
        self.container_created = True
        # docker takes the key's value from its own environment; its argv carries the name only.
        if not self.call(check, "docker run", ["docker", "run", "-d", "--name", DRILL_CONTAINER, "-e", KEY_VARIABLE,
                                               "-p", f"127.0.0.1:{DRILL_QDRANT_PORT}:6333", "--memory", "1g",
                                               self.image], env={**os.environ, KEY_VARIABLE: self.key}):
            return False
        self.ok(f"{check}: 127.0.0.1:{DRILL_QDRANT_PORT}, 1 GiB, no volume, a key of its own")
        return True

    def restore_databases(self) -> List[str]:
        """Each dump restored through the restore's own command; the databases restored."""
        env = {**os.environ, "RMP_PG_SOCKET_DIR": str(self.pg), "RMP_PG_PORT": str(PG_PORT)}
        restored = []
        for db in OWNERS:
            dump = self.backup / f"{db}.dump"
            if not dump.is_file() or dump.stat().st_size == 0:
                self.fail(f"restore {db}: {dump.name} is missing or empty")
            elif self.call(f"restore {db}", "restore_pg_database.sh",
                           ["bash", str(REPO / "ops" / "restore_pg_database.sh"), str(dump)], env=env):
                self.ok(f"restore {db}")
                restored.append(db)
        return restored

    def restore_qdrant(self) -> bool:
        # TMPDIR: the script extracts the collection snapshots into a work directory in the scratch directory, which
        # the cleanup removes even when a signal kills the script before its own trap can.
        if not self.call("restore qdrant", "restore_qdrant_snapshot.sh",
                         ["bash", str(REPO / "ops" / "restore_qdrant_snapshot.sh"), str(self.snapshot), QDRANT_URL],
                         env={**os.environ, "QDRANT_API_KEY": self.key, "TMPDIR": str(self.scratch)}):
            return False
        self.ok("restore qdrant")
        return True

    def check_database(self, db: str) -> None:
        """Its owner, rmp_db's vector extension, and every table's count(*) against the rows of its COPY block."""
        listed = self.call(f"{db} owner", "psql", self.psql(
            "postgres", f"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '{db}'"))
        if listed is not None:
            owner = listed.stdout.decode(errors="replace").strip()
            if owner == OWNERS[db]:
                self.ok(f"{db} owner: {owner}")
            else:
                self.fail(f"{db} owner: {owner}, not {OWNERS[db]}" if owner else f"{db} owner: no such database")
        if db == "rmp_db":
            listed = self.call(f"{db} extension", "psql", self.psql(
                db, "SELECT extname FROM pg_extension WHERE extname = 'vector'"))
            if listed is not None:
                if listed.stdout.split() == [b"vector"]:
                    self.ok(f"{db} extension: vector")
                else:
                    self.fail(f"{db} extension: no vector")
        self.check_rows(db)

    def check_rows(self, db: str) -> None:
        """root reads the dump's data as SQL; no server is involved."""
        check = f"{db} rows"
        dumped = self.call(check, "pg_restore", [f"{self.pg_bin}/pg_restore", "--data-only", "-f", "-",
                                                 str(self.backup / f"{db}.dump")])
        if dumped is None:
            return
        try:
            copied = copy_rows(io.BytesIO(dumped.stdout))
        except ValueError as exc:
            self.fail(f"{check}: {exc}")
            return
        listed = self.call(check, "psql", self.psql(db, COUNT_QUERY))
        if listed is None:
            return
        counted = {}
        for line in listed.stdout.decode(errors="replace").splitlines():
            table, _, rows = line.rpartition("|")
            counted[table] = int(rows)
        mismatches = row_mismatches(counted, copied)
        if mismatches:
            self.fail(f"{check}: {'; '.join(mismatches)}")
        else:
            self.ok(f"{check}: {len(counted)} tables, {sum(counted.values())} rows, each as in its COPY block")

    def qdrant_get(self, check: str, path: str) -> Optional[dict]:
        """The result of a GET on the scratch Qdrant, or None after a FAIL line. The key goes to curl on stdin."""
        answer = self.call(check, "curl", ["curl", "-fsS", "-m", "30", "-K", "-", f"{QDRANT_URL}{path}"],
                           input=f'header = "api-key: {self.key}"\n'.encode())
        if answer is None:
            return None
        try:
            return json.loads(answer.stdout)["result"]
        except (ValueError, KeyError, TypeError):
            self.fail(f"{check}: Qdrant's answer has no result")
            return None

    def check_collections(self) -> None:
        """The collections Qdrant has against those of the snapshot, and each one's points."""
        check = "qdrant collections"
        try:
            expected = snapshot_collections(self.snapshot)
        except (OSError, tarfile.TarError) as exc:
            self.fail(f"{check}: the snapshot does not list ({type(exc).__name__})")
            return
        listed = self.qdrant_get(check, "/collections")
        if listed is None:
            return
        names = {collection.get("name") for collection in listed.get("collections") or []}
        if names == expected:
            self.ok(f"{check}: {len(names)}, as in the snapshot")
        else:
            missing, extra = ", ".join(sorted(expected - names)), ", ".join(sorted(map(str, names - expected)))
            self.fail(f"{check}: missing {missing or 'none'}; not in the snapshot {extra or 'none'}")
        for name in sorted(names & expected):
            info = self.qdrant_get(f"qdrant {name}", f"/collections/{urllib.parse.quote(name, safe='')}")
            if info is not None:
                self.ok(f"qdrant {name}: {info.get('points_count')} points")

    def check_openclaw(self) -> None:
        """OpenClaw's store copies: each matches its checksum and passes quick_check."""
        try:
            manifest = backup_openclaw_state.verify(self.backup / "openclaw-state")
        except Exception as exc:  # whatever stops the check fails it
            self.fail(f"openclaw-state: {str(exc) or type(exc).__name__}")
        else:
            self.ok(f"openclaw-state: {', '.join(manifest['files'])}, checksums match, quick_check ok")

    def check_archives(self) -> None:
        for archive in sorted(self.backup.glob("*.tar.gz")):
            try:
                entries = archive_entries(archive)
            except (OSError, EOFError, tarfile.TarError, zlib.error) as exc:
                self.fail(f"archive {archive.name}: does not read to its end ({type(exc).__name__})")
            else:
                self.ok(f"archive {archive.name}: {entries} entries, read to its end")

    def cleanup(self) -> None:
        """The container, the server, then the scratch directory, each removed even when one before it was not."""
        left = []

        def removed(argv: List[str]) -> bool:
            try:
                return self.run(argv).returncode == 0
            except OSError:
                return False

        if self.container_created and not removed(["docker", "rm", "-f", DRILL_CONTAINER]):
            left.append(f"the container {DRILL_CONTAINER}")
        if self.pg_started:
            stopped = removed(self.as_postgres("pg_ctl", "-D", str(self.pg / "data"), "stop", "-m", "fast"))
            # pg_ctl stop fails too when no server runs, as after a failed start; a running server has its pid file.
            if not stopped and os.path.exists(self.pg / "data" / "postmaster.pid"):
                left.append("the scratch server")
        if self.scratch is not None:
            shutil.rmtree(self.scratch, ignore_errors=True)
            if os.path.lexists(self.scratch):
                left.append(str(self.scratch))
        if left:
            self.fail(f"cleanup: could not remove {', '.join(left)}; remove what is left with: {HARD_KILL_CLEANUP}")


def main(argv: Optional[Sequence[str]] = None, *, run: Run = _run, probes: Optional[Probes] = None,
         scratch_root: Path = SCRATCH_ROOT, backups: Optional[Path] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("backup", nargs="?", type=Path,
                        help='the backup directory to drill (default: the newest whose manifest has "failures": [])')
    args = parser.parse_args(argv)
    root = backups or Path(RMP_DATA_DIR) / "backups"
    # Absolute: every command runs from /.
    backup = args.backup.absolute() if args.backup else newest_clean(root)
    if backup is None:
        print(f'refused: no backup under {root} has a manifest with "failures": []')
        return 2
    if not backup.is_dir():
        print(f"refused: {backup} is not a directory")
        return 2
    reasons = refusals(run, probes or Probes())
    image = qdrant_image()
    if image is None:
        reasons.append("ops/start_qdrant.sh names no single Qdrant image")
    if reasons:
        for reason in reasons:
            print(f"refused: {reason}")
        return 2
    print(f"backup: {backup}", flush=True)
    # The backup against its manifest first, checksums included: one that does not verify is not drilled, and the
    # drill creates nothing.
    problems = backup_manifest.problems(backup, hashes=True)
    for problem in problems:
        print(f"FAIL backup manifest: {problem}", flush=True)
    if problems:
        return 1
    print("ok backup manifest: every file's size and checksum match", flush=True)
    # A hangup the caller ignores, as nohup does, stays ignored: the drill then outlives its session.
    handled = [sig for sig in SIGNALS if not (sig == signal.SIGHUP and signal.getsignal(sig) == signal.SIG_IGN)]
    previous = {sig: signal.signal(sig, _exit_on_signal) for sig in handled}
    try:
        return Drill(backup, run=run, scratch_root=scratch_root, pg_bin=os.environ.get("RMP_PG_BIN", PG_BIN),
                     image=image).perform()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
