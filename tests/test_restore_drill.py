"""ops/restore_drill.py, the restore drill, against an injected recording runner and injected host probes.

Nothing here starts a server or a container, connects anywhere or reads production. Every external command goes to
the recording runner, which records its argv, environment and stdin and answers as the host would. The backup and the
scratch root are in tmp_path, and memory, idleness and the drill port come from injected probes. A real connect to
6937 would be refused anyway: the production guard treats every blocked port as a service port.
"""
import io
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.coding.firewall import covers, port_elements
from app.config import DEFAULT_CODING
from ops import backup_openclaw_state
from ops import restore_drill as drill

REPO = Path(__file__).resolve().parents[1]
STAMP = "20261019T031502Z"
COLLECTIONS = {"rmp_memories": 120, "rmp_task_registry": 7}
# Each database's tables and rows, as pg_restore --data-only prints them and count(*) counts them.
TABLES = {"rmp_db": {"public.tasks": 3, "public.events": 0, 'public."Task Notes"': 2},
          "temporal": {"public.executions": 4, "public.history_node": 5},
          "temporal_visibility": {"public.executions_visibility": 4}}
OWNERS = {"rmp_db": "rmp", "temporal": "temporal", "temporal_visibility": "temporal"}
HARD_KILL = ('docker rm -f rmp-qdrant-drill; for d in /var/tmp/rmp-restore-drill-*; do sudo -n -u postgres '
             '/usr/lib/postgresql/16/bin/pg_ctl -D "$d/pg/data" stop -m fast; rm -rf "$d"; done')
IDLE = drill.Probes(euid=lambda: 0, mem_available=lambda: 4 << 30, active_user_tasks=lambda: 0,
                    port_taken=lambda port: False)


def copy_stream(tables):
    """pg_restore --data-only -f - of a dump holding ``tables``: a COPY block per table, among other SQL. A value
    holds a tab and an escaped newline, as COPY's text format writes them."""
    out = [b"--\n-- PostgreSQL database dump\n--\n\nSET statement_timeout = 0;\n\n"]
    for table, rows in tables.items():
        out.append(f"--\n-- Data for Name: {table}; Type: TABLE DATA\n--\n\n".encode())
        out.append(f"COPY {table} (id, body) FROM stdin;\n".encode())
        out += [f"{n}\tfirst line\\nsecond\\tline\n".encode() for n in range(rows)]
        out.append(b"\\.\n\n\n")
    out.append(b"SELECT pg_catalog.setval('public.tasks_id_seq', 3, true);\n")
    return b"".join(out)


class Host:
    """The recording runner: every call's argv, environment and stdin, answered as the host would.

    ``fail`` names a dump whose restore_pg_database.sh exits 1, ``counts`` overrides a database's count(*) answers,
    ``units`` is what systemctl lists as active coding units, ``containers`` what docker lists, and ``on`` maps a
    command's name to what to do while the runner handles it; what that returns, unless None, is the command's exit
    code.
    """

    def __init__(self, *, fail=None, counts=None, units="", containers="", on=None):
        self.fail, self.counts, self.units, self.containers = fail, counts or {}, units, containers
        self.on, self.calls = on or {}, []

    def __call__(self, argv, *, env=None, input=None):
        argv = list(argv)
        self.calls.append(SimpleNamespace(argv=argv, env=env, input=input))
        words = argv[4:] if argv[0] == "sudo" else argv
        name = Path(words[1] if words[0] == "bash" else words[0]).name
        exited = self.on[name]() if name in self.on else None
        returncode, stdout = self.answer(name, words)
        return subprocess.CompletedProcess(argv, returncode if exited is None else exited, stdout, b"")

    def answer(self, name, words):
        if name == "systemctl":
            return 0, self.units.encode()
        if name == "docker":
            return 0, {"ps": self.containers.encode(), "run": b"f00d\n"}.get(words[1], b"")
        if name == "restore_pg_database.sh":
            return (1 if Path(words[2]).name == self.fail else 0), b""
        if name == "pg_restore":
            return 0, copy_stream(TABLES[Path(words[-1]).stem])
        if name == "psql":
            sql, db = words[words.index("-c") + 1], words[words.index("-d") + 1]
            if words[-4:] == ["-c", drill.ROLE_QUERY[0], "-c", drill.ROLE_QUERY[1]]:
                return 0, b"rmp\ntemporal\n"
            if "pg_database" in sql:
                return 0, (OWNERS[re.search(r"datname = '(\w+)'", sql).group(1)] + "\n").encode()
            if "pg_extension" in sql:
                return 0, b"vector\n"
            if sql == drill.COUNT_QUERY:
                counted = {**TABLES[db], **self.counts.get(db, {})}
                return 0, "".join(f"{table}|{rows}\n" for table, rows in counted.items()).encode()
        if name == "curl":
            path = urllib.parse.urlsplit(words[-1]).path
            if path == "/collections":
                return 0, json.dumps({"result": {"collections": [{"name": c} for c in COLLECTIONS]}}).encode()
            collection = urllib.parse.unquote(path.rsplit("/", 1)[1])
            return 0, json.dumps({"result": {"points_count": COLLECTIONS[collection]}}).encode()
        return 0, b""

    def named(self, name):
        """The calls of one command: a program, or a script bash runs."""
        def called(call):
            words = call.argv[4:] if call.argv[0] == "sudo" else call.argv
            return Path(words[1] if words[0] == "bash" else words[0]).name == name
        return [call for call in self.calls if called(call)]


def _store(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE events (n INTEGER)")
        con.executemany("INSERT INTO events VALUES (?)", [(n,) for n in range(rows)])
        con.commit()
    finally:
        con.close()


def _add(tar, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


@pytest.fixture(autouse=True)
def pg_bin(tmp_path, monkeypatch):
    """The tests never name the host's PostgreSQL binaries."""
    monkeypatch.setenv("RMP_PG_BIN", str(tmp_path / "pg-bin"))
    return tmp_path / "pg-bin"


@pytest.fixture
def backup(tmp_path):
    """A clean backup as ops/backup.sh leaves it, with real OpenClaw store copies, snapshot and archive."""
    dest = tmp_path / "backups" / STAMP
    dest.mkdir(parents=True)
    (dest / "manifest.json").write_text(json.dumps({"timestamp": STAMP, "failures": []}))
    for db in OWNERS:
        (dest / f"{db}.dump").write_bytes(b"PGDMP" + db.encode())
    with tarfile.open(dest / "qdrant-full.snapshot", "w") as tar:
        for collection in COLLECTIONS:
            _add(tar, f"{collection}-5430049239505477-2026-10-19-03-15-07.snapshot", collection.encode())
        _add(tar, "config.json", b"{}")
    with tarfile.open(dest / "artifacts.tar.gz", "w:gz") as tar:
        _add(tar, "artifacts/a.json", b"{}")
        _add(tar, "artifacts/b.json", b"{}")
    home = tmp_path / "openclaw"
    for rows, store in enumerate(backup_openclaw_state.stores(home).values(), start=2):
        _store(store, rows)
    backup_openclaw_state.backup(dest / "openclaw-state", home=home, version="OpenClaw test")
    return dest


@pytest.fixture
def signals():
    """SIGHUP not ignored, as a terminal session starts the drill; the test process's own handlers again
    afterwards."""
    before = {sig: signal.getsignal(sig) for sig in drill.SIGNALS}
    signal.signal(signal.SIGHUP, signal.SIG_DFL)
    yield
    for sig, handler in before.items():
        signal.signal(sig, handler)


@pytest.fixture
def scratch_root(tmp_path):
    root = tmp_path / "scratch"
    root.mkdir()
    return root


def run_drill(capsys, backup, scratch_root, host, probes=IDLE, argv=None):
    """The drill's exit code and its output lines."""
    code = drill.main([str(backup)] if argv is None else argv, run=host, probes=probes, scratch_root=scratch_root,
                      backups=backup.parent)
    return code, capsys.readouterr().out.splitlines()


def test_a_drill_of_a_whole_backup_checks_every_store_and_leaves_nothing_behind(capsys, backup, scratch_root, pg_bin):
    host = Host()
    code, lines = run_drill(capsys, backup, scratch_root, host)
    assert code == 0, lines
    assert lines[:-1] == [
        f"backup: {backup}",
        "ok scratch postgres: port 55432, socket only, production's roles rmp, temporal NOLOGIN",
        "ok scratch qdrant: 127.0.0.1:6937, 1 GiB, no volume, a key of its own",
        "ok restore rmp_db",
        "ok restore temporal",
        "ok restore temporal_visibility",
        "ok restore qdrant",
        "ok rmp_db owner: rmp",
        "ok rmp_db extension: vector",
        "ok rmp_db rows: 3 tables, 5 rows, each as in its COPY block",
        "ok temporal owner: temporal",
        "ok temporal rows: 2 tables, 9 rows, each as in its COPY block",
        "ok temporal_visibility owner: temporal",
        "ok temporal_visibility rows: 1 tables, 4 rows, each as in its COPY block",
        "ok qdrant collections: 2, as in the snapshot",
        "ok qdrant rmp_memories: 120 points",
        "ok qdrant rmp_task_registry: 7 points",
        "ok openclaw-state: agent, state, checksums match, quick_check ok",
        "ok archive artifacts.tar.gz: 2 entries, read to its end",
    ]
    assert re.fullmatch(r"restore duration: \d+ min \d+ s", lines[-1])
    # The roles are created on the scratch server only, without a login.
    create = [c.argv[-1] for c in host.named("psql") if "CREATE ROLE" in c.argv[-1]]
    assert create == ['CREATE ROLE "rmp" NOLOGIN; CREATE ROLE "temporal" NOLOGIN']
    # The dumps are read as root, never through a server.
    assert [c.argv for c in host.named("pg_restore")] == [
        [f"{pg_bin}/pg_restore", "--data-only", "-f", "-", str(backup / f"{db}.dump")]
        for db in OWNERS]
    # Cleanup: the container, then the server, then the scratch directory.
    assert [c.argv for c in host.calls[-2:]] == [
        ["docker", "rm", "-f", "rmp-qdrant-drill"],
        host.named("pg_ctl")[-1].argv]
    assert host.named("pg_ctl")[-1].argv[-3:] == ["stop", "-m", "fast"]
    assert list(scratch_root.iterdir()) == []


def test_the_drill_only_ever_targets_its_scratch_instances(capsys, backup, scratch_root, pg_bin):
    modes = {}

    def scratch_modes():
        [scratch] = scratch_root.iterdir()
        modes.update(scratch=stat.S_IMODE(scratch.stat().st_mode), pg=stat.S_IMODE((scratch / "pg").stat().st_mode))

    host = Host(on={"chown": scratch_modes})
    code, lines = run_drill(capsys, backup, scratch_root, host)
    assert code == 0, lines
    [chown] = host.named("chown")
    pg = Path(chown.argv[-1])
    assert chown.argv == ["chown", "postgres:postgres", str(pg)]
    assert pg.name == "pg" and pg.parent.parent == scratch_root and pg.parent.name.startswith("rmp-restore-drill-")
    assert modes == {"scratch": 0o711, "pg": 0o700}
    data = str(pg / "data")
    # The one production read: the role names, read-only.
    [roles] = [c for c in host.named("psql") if "-h" not in c.argv]
    assert roles.argv[:5] == ["sudo", "-n", "-u", "postgres", f"{pg_bin}/psql"]
    # The SET is a -c of its own before the SELECT: sharing one -c, both run in one transaction that is not read-only.
    assert roles.argv[-4:] == ["-c", "SET default_transaction_read_only=on", "-c", drill.ROLE_QUERY[1]]
    assert drill.ROLE_QUERY[1].startswith("SELECT rolname FROM pg_roles ")
    assert roles.argv[roles.argv.index("-d") + 1] == "postgres" and "-p" not in roles.argv
    # -q: without it psql prints SET's status line among the names, and the drill would create a role "SET".
    assert {"-At", "-q", "-X"} <= set(roles.argv)
    # Every other server call names the scratch directory, and psql its socket and port.
    server_calls = [c for name in ("initdb", "pg_ctl", "psql") for c in host.named(name) if c is not roles]
    assert len(server_calls) == 1 + 2 + 1 + 3 + 1 + 3
    for call in server_calls:
        assert call.argv[:4] == ["sudo", "-n", "-u", "postgres"] and call.argv[4].startswith(f"{pg_bin}/")
        assert any(word.startswith(str(pg)) for word in call.argv), call.argv
        if call.argv[4].endswith("/psql"):
            assert call.argv[5:9] == ["-h", str(pg), "-p", "55432"], call.argv
    [initdb] = host.named("initdb")
    assert initdb.argv[5:] == ["-D", data, "--auth=peer", "--username=postgres", "--encoding=UTF8"]
    start, stop = host.named("pg_ctl")
    assert start.argv[5:] == ["-D", data, "-l", str(pg / "server.log"), "-w", "-o",
                              f"-c listen_addresses='' -c port=55432 -c unix_socket_directories={pg}", "start"]
    assert stop.argv[5:] == ["-D", data, "stop", "-m", "fast"]
    for call in host.calls:
        assert not any(word == "5432" or "port=5432 " in word or "/var/run/postgresql" in word for word in call.argv)
    # Each dump through the restore's own command, pointed at the scratch server.
    restores = host.named("restore_pg_database.sh")
    assert [c.argv for c in restores] == [
        ["bash", str(REPO / "ops" / "restore_pg_database.sh"), str(backup / f"{db}.dump")] for db in OWNERS]
    for call in restores:
        assert call.env["RMP_PG_SOCKET_DIR"] == str(pg) and call.env["RMP_PG_PORT"] == "55432"
    # Every docker call names the drill's container, never production's.
    docker = host.named("docker")
    assert [c.argv[1] for c in docker] == ["ps", "run", "rm"]
    for call in docker:
        assert any(word in ("rmp-qdrant-drill", "name=rmp-qdrant-drill") for word in call.argv)
        assert "rmp-qdrant" not in call.argv
    run = docker[1].argv
    assert run[:4] == ["docker", "run", "-d", "--name"] and run[4] == "rmp-qdrant-drill"
    assert run[run.index("-p") + 1] == "127.0.0.1:6937:6333" and run[run.index("--memory") + 1] == "1g"
    assert run[-1] == drill.qdrant_image() and not any(word in ("-v", "--volume", "--mount") for word in run)
    # The Qdrant URL is the drill's, and the snapshot restore's work directory goes in the scratch directory.
    [qdrant] = host.named("restore_qdrant_snapshot.sh")
    assert qdrant.argv[1:] == [str(REPO / "ops" / "restore_qdrant_snapshot.sh"), str(backup / "qdrant-full.snapshot"),
                               "http://127.0.0.1:6937"]
    assert qdrant.env["TMPDIR"] == str(pg.parent)
    assert {c.argv[-1].rsplit("/", 1)[0] for c in host.named("curl")} == {
        "http://127.0.0.1:6937", "http://127.0.0.1:6937/collections"}


def test_the_drill_runs_the_qdrant_image_of_start_qdrant():
    image = drill.qdrant_image()
    assert image == "qdrant/qdrant:v1.17.0" and image in (REPO / "ops" / "start_qdrant.sh").read_text()


def test_the_drill_removes_its_scratch_instances_when_a_step_fails(capsys, backup, scratch_root):
    host = Host(fail="temporal.dump")
    code, lines = run_drill(capsys, backup, scratch_root, host)
    assert code == 1
    assert "FAIL restore temporal: restore_pg_database.sh exited 1" in lines
    assert "ok restore temporal_visibility" in lines and not any(line.startswith("ok temporal ") for line in lines)
    assert re.fullmatch(r"restore duration: \d+ min \d+ s", lines[-1])
    assert ["docker", "rm", "-f", "rmp-qdrant-drill"] in [c.argv for c in host.calls]
    assert host.named("pg_ctl")[-1].argv[-3:] == ["stop", "-m", "fast"]
    assert list(scratch_root.iterdir()) == []


@pytest.mark.parametrize("server_left", [False, True], ids=["no server", "a server left"])
def test_a_failed_stop_reports_the_scratch_server_only_when_one_is_left(capsys, backup, scratch_root, server_left):
    """pg_ctl start fails, and pg_ctl stop with it, as it does when no server runs. The cleanup names the server as
    left, with the command that removes it, only when a server has its postmaster.pid."""
    def pg_ctl():
        argv = host.calls[-1].argv
        if argv[-1] == "start" and server_left:
            data = Path(argv[argv.index("-D") + 1])
            data.mkdir()
            (data / "postmaster.pid").write_text("4242\n")
        return 1

    host = Host(on={"pg_ctl": pg_ctl})
    code, lines = run_drill(capsys, backup, scratch_root, host)
    assert code == 1
    assert "FAIL scratch postgres: pg_ctl start exited 1" in lines
    assert host.named("pg_ctl")[-1].argv[-3:] == ["stop", "-m", "fast"]
    left = f"FAIL cleanup: could not remove the scratch server; remove what is left with: {HARD_KILL}"
    assert [line for line in lines if line.startswith("FAIL cleanup")] == ([left] if server_left else [])
    assert list(scratch_root.iterdir()) == []


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP], ids=lambda sig: sig.name)
def test_a_signal_ends_the_drill_through_its_cleanup(capsys, backup, scratch_root, signals, sig):
    """A kill, Ctrl-C or a hung-up session during the Qdrant restore: both instances and the scratch directory go,
    with the snapshot restore's work directory, and the signal's handlers are the caller's again afterwards."""
    before = {s: signal.getsignal(s) for s in drill.SIGNALS}
    work = []

    def restore_cut_short():
        # The script's mktemp -d makes its work directory in its TMPDIR; the signal kills it before its trap runs.
        work.append(tempfile.mkdtemp(dir=host.calls[-1].env.get("TMPDIR")))
        os.kill(os.getpid(), sig)

    host = Host(on={"restore_qdrant_snapshot.sh": restore_cut_short})
    with pytest.raises(SystemExit) as ended:
        run_drill(capsys, backup, scratch_root, host)
    assert ended.value.code == 128 + sig
    assert [c.argv[:2] for c in host.calls[-2:]] == [["docker", "rm"], ["sudo", "-n"]]
    assert host.calls[-1].argv[-3:] == ["stop", "-m", "fast"]
    assert list(scratch_root.iterdir()) == [] and not os.path.lexists(work[0])
    assert "restore duration" not in capsys.readouterr().out
    assert {s: signal.getsignal(s) for s in drill.SIGNALS} == before


def test_a_hangup_ignored_as_under_nohup_stays_ignored(capsys, backup, scratch_root, signals):
    """nohup starts the drill with SIGHUP ignored: a hangup then leaves it running to its end."""
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    host = Host(on={"restore_qdrant_snapshot.sh": lambda: os.kill(os.getpid(), signal.SIGHUP)})
    code, lines = run_drill(capsys, backup, scratch_root, host)
    assert code == 0, lines
    assert "ok qdrant collections: 2, as in the snapshot" in lines
    assert re.fullmatch(r"restore duration: \d+ min \d+ s", lines[-1])
    assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN


@pytest.mark.parametrize("ended_by", [None, signal.SIGTERM], ids=["a whole drill", "a drill ended by SIGTERM"])
def test_a_signal_during_the_cleanup_is_ignored(backup, scratch_root, ended_by):
    """Ctrl-C while the container is removed: the server is still stopped and the scratch directory removed, and
    the drill ends as it would have without it."""
    def ctrl_c_during_docker_rm():
        if host.calls[-1].argv[1] == "rm":
            os.kill(os.getpid(), signal.SIGINT)

    on = {"docker": ctrl_c_during_docker_rm}
    if ended_by:
        on["restore_qdrant_snapshot.sh"] = lambda: os.kill(os.getpid(), ended_by)
    host = Host(on=on)
    try:
        code = drill.main([str(backup)], run=host, probes=IDLE, scratch_root=scratch_root, backups=backup.parent)
    except SystemExit as ended:
        code = ended.code
    assert code == (128 + ended_by if ended_by else 0)
    assert [c.argv[:2] for c in host.calls[-2:]] == [["docker", "rm"], ["sudo", "-n"]]
    assert host.calls[-1].argv[-3:] == ["stop", "-m", "fast"]
    assert list(scratch_root.iterdir()) == []


def test_a_signal_handled_as_the_cleanup_begins_ends_the_drill_after_it(capsys, backup, scratch_root, monkeypatch):
    """A SIGTERM whose handler runs as the cleanup begins, before the signals are ignored: the cleanup still runs
    whole, then the drill ends with the signal's code."""
    hold = drill._hold_signals

    def handled_first():
        monkeypatch.setattr(drill, "_hold_signals", hold)
        os.kill(os.getpid(), signal.SIGTERM)   # its handler runs here
        hold()

    monkeypatch.setattr(drill, "_hold_signals", handled_first)
    host = Host()
    with pytest.raises(SystemExit) as ended:
        run_drill(capsys, backup, scratch_root, host)
    assert ended.value.code == 128 + signal.SIGTERM
    assert [c.argv[:2] for c in host.calls[-2:]] == [["docker", "rm"], ["sudo", "-n"]]
    assert host.calls[-1].argv[-3:] == ["stop", "-m", "fast"]
    assert list(scratch_root.iterdir()) == []
    assert "restore duration" not in capsys.readouterr().out


def test_a_row_count_mismatch_fails_the_drill(capsys, backup, scratch_root):
    stream = io.BytesIO(copy_stream({"public.tasks": 3, "public.events": 0, 'public."Task Notes"': 2}))
    assert drill.copy_rows(stream) == {"public.tasks": 3, "public.events": 0, 'public."Task Notes"': 2}
    with pytest.raises(ValueError, match="the COPY block of public.tasks has no end"):
        drill.copy_rows(io.BytesIO(b"COPY public.tasks (id) FROM stdin;\n1\n2\n"))
    assert drill.row_mismatches({"public.tasks": 3, "public.extra": 0}, {"public.tasks": 2, "public.events": 0}) == [
        "public.events is not in the restored database", "public.extra has no COPY block",
        "public.tasks has 3 rows, its COPY block 2"]
    host = Host(counts={"temporal": {"public.history_node": 4}})
    code, lines = run_drill(capsys, backup, scratch_root, host)
    assert code == 1
    assert "FAIL temporal rows: public.history_node has 4 rows, its COPY block 5" in lines
    assert "ok rmp_db rows: 3 tables, 5 rows, each as in its COPY block" in lines
    assert list(scratch_root.iterdir()) == []


def test_a_damaged_store_copy_or_archive_fails_its_check(capsys, backup, scratch_root):
    copy = backup / "openclaw-state" / "openclaw.sqlite"
    copy.write_bytes(copy.read_bytes()[:-1] + b"x")
    archive = backup / "artifacts.tar.gz"
    archive.write_bytes(archive.read_bytes()[:-8])
    code, lines = run_drill(capsys, backup, scratch_root, Host())
    assert code == 1
    assert f"FAIL openclaw-state: {copy} does not match its manifest checksum" in lines
    assert "FAIL archive artifacts.tar.gz: does not read to its end (EOFError)" in lines


def _outside_the_drill_port():
    return {**DEFAULT_CODING, "blocked_tcp_ports": [22, 5432, "6933-6936", "6938-6939"]}


def _lookup_failed():
    raise ConnectionRefusedError("the database is down")


# Each unsafe start: the probes and runner answers that make it, and the one line the drill prints.
UNSAFE_STARTS = [
    ("a taken port", {"port_taken": lambda port: port == 6937}, {},
     "something accepts connections on 127.0.0.1:6937"),
    ("a port outside blocked_tcp_ports", {}, {},
     "port 6937 is not in coding.blocked_tcp_ports, so aura-coder could reach the drill's Qdrant"),
    ("low memory", {"mem_available": lambda: (2 << 30) - (1 << 20)}, {},
     "MemAvailable is 2047 MiB; the drill needs 2048 MiB"),
    ("one active user task", {"active_user_tasks": lambda: 1}, {},
     "1 user task(s) active; run the drill when Aura is idle"),
    ("a failed task lookup", {"active_user_tasks": _lookup_failed}, {},
     "could not count the active user tasks (ConnectionRefusedError); that counts as busy"),
    ("an active aura-claude-* unit", {}, {"units": "aura-claude-t1.service loaded active running Claude t1\n"},
     "aura-claude-t1.service active; run the drill when no coding unit runs"),
    ("not root", {"euid": lambda: 1000}, {}, "the drill runs as root"),
    ("a drill container left behind", {}, {"containers": "rmp-qdrant-drill\n"},
     f"a container named rmp-qdrant-drill exists; an earlier drill was killed: {HARD_KILL}"),
]


@pytest.mark.parametrize("case, probes, host, refusal", UNSAFE_STARTS, ids=[start[0] for start in UNSAFE_STARTS])
def test_the_drill_refuses_an_unsafe_start(capsys, backup, scratch_root, monkeypatch, case, probes, host, refusal):
    if case == "a port outside blocked_tcp_ports":
        monkeypatch.setattr(drill, "get_coding_config", _outside_the_drill_port)
    host = Host(**host)
    code, lines = run_drill(capsys, backup, scratch_root, host, probes=drill.Probes(**{**vars(IDLE), **probes}))
    assert code == 2
    assert lines == [f"refused: {refusal}"]
    assert host.named("initdb") == [] and host.named("chown") == []
    # Only the read-only listings ran: no container, server or directory was created.
    assert [c.argv[:2] for c in host.calls] == [["systemctl", "list-units"], ["docker", "ps"]]
    assert host.calls[0].argv[-5:] == ["aura-claude-*", "aura-verify-*", "aura-collect-*", "aura-direct-*",
                                       "aura-deploy-*"]
    assert list(scratch_root.iterdir()) == []


def test_the_drill_drills_the_newest_backup_whose_manifest_has_no_failures(capsys, backup, scratch_root):
    root = backup.parent
    for stamp, manifest in (("20261020T031501Z", {"failures": ["qdrant"]}),   # newer, failed
                            ("20261021T031501Z", None),                       # newest, unfinished
                            ("20261018T031501Z", {"failures": []})):          # older, clean
        (root / stamp).mkdir()
        if manifest is not None:
            (root / stamp / "manifest.json").write_text(json.dumps(manifest))
    (root / "openclaw-update-20261022T000000Z").mkdir()
    (root / "openclaw-update-20261022T000000Z" / "manifest.json").write_text(json.dumps({"failures": []}))
    assert drill.newest_clean(root) == backup
    code, lines = run_drill(capsys, backup, scratch_root, Host(), argv=[])
    assert code == 0 and lines[0] == f"backup: {backup}"
    for name in [backup.name, "20261018T031501Z"]:
        (root / name / "manifest.json").write_text(json.dumps({"timestamp": name}))   # before the failures list
    assert drill.newest_clean(root) is None
    host = Host()
    code, lines = run_drill(capsys, backup, scratch_root, host, argv=[])
    assert code == 2 and lines == [f'refused: no backup under {root} has a manifest with "failures": []']
    assert host.calls == []


def test_a_backup_given_by_a_relative_path_is_drilled_by_its_absolute_path(capsys, backup, scratch_root, monkeypatch):
    """Every command runs from /, so a relative path would name nothing there."""
    monkeypatch.chdir(backup.parent)
    host = Host()
    code, lines = run_drill(capsys, backup, scratch_root, host, argv=[backup.name])
    assert code == 0 and lines[0] == f"backup: {backup}"
    assert [c.argv[-1] for c in host.named("restore_pg_database.sh")] == [str(backup / f"{db}.dump") for db in OWNERS]


def test_the_drill_port_is_closed_to_aura_coder_and_its_key_never_reaches_argv(capsys, backup, scratch_root):
    assert covers(port_elements(DEFAULT_CODING["blocked_tcp_ports"]), drill.DRILL_QDRANT_PORT)
    host = Host()
    code, lines = run_drill(capsys, backup, scratch_root, host)
    assert code == 0, lines
    [run] = [c for c in host.named("docker") if c.argv[1] == "run"]
    assert run.argv[run.argv.index("-e") + 1] == "QDRANT__SERVICE__API_KEY"
    key = run.env["QDRANT__SERVICE__API_KEY"]
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    [restore] = host.named("restore_qdrant_snapshot.sh")
    assert restore.env["QDRANT_API_KEY"] == key
    curls = host.named("curl")
    assert len(curls) == 1 + len(COLLECTIONS)
    for call in curls:
        assert call.argv[1:5] == ["-fsS", "-m", "30", "-K"] and call.argv[5] == "-"
        assert call.input == f'header = "api-key: {key}"\n'.encode()
    # The key is in the environment of these two calls only, on no command line, and in no output.
    assert [c for c in host.calls if c.env and key in c.env.values()] == [run, restore]
    assert not any(key in word for call in host.calls for word in call.argv)
    assert key not in "\n".join(lines) and key not in os.environ.values()


def test_the_docs_give_the_drill_and_its_hard_kill_cleanup():
    runbook = (REPO / "docs" / "runbooks" / "backup-restore.md").read_text()
    drill_section = runbook.split("## Quarterly DR drill", 1)[1]
    assert "cd /root/.openclaw/rmp && venv/bin/python ops/restore_drill.py" in drill_section
    assert HARD_KILL in drill_section
    assert drill.HARD_KILL_CLEANUP == HARD_KILL and HARD_KILL in drill.__doc__
