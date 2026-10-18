"""ops/backup.sh and the restore scripts end to end, against fake pg_dump, pg_restore, sudo, systemctl and Qdrant API.

The scripts run from outside the checkout (as rmp-backup.service does, with no working directory), with their data,
settings and OpenClaw home in the test's tmp tree, so nothing live is read. A script runs only when every tool it
could reach on the host resolves to its fake, and a restore only when it takes every path it writes from the
environment: the production guard does not see a child process's own calls.
"""
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BACKUP = REPO / "ops" / "backup.sh"
RESTORE = REPO / "ops" / "restore_backup.sh"
RESTORE_PG = REPO / "ops" / "restore_pg_database.sh"
RESTORE_QDRANT = REPO / "ops" / "restore_qdrant_snapshot.sh"
# Every path the restore writes or locks comes from one of these, each with a default (ops/backup.sh's names).
RESTORE_PATHS = ("RMP_ROOT", "RMP_DATA_DIR", "RMP_SETTINGS_PATH", "RMP_PYTHON", "RMP_CODE_RELOAD_LOCK")
# The RMP timers enabled on the host; the fake systemctl lists them as active.
TIMERS = sorted(u for u in (REPO / "ops" / "systemd" / "enabled-units.txt").read_text().splitlines()
                if u.endswith(".timer"))
# The one restore command of every database: the superuser drops it and recreates it from its archive.
PG_RESTORE = ["pg_restore", "-h", "/var/run/postgresql", "-p", "5432", "--clean", "--if-exists", "--create",
              "--exit-on-error", "-d", "postgres"]
# What the restore stops, in order: each timer and its service, the code watch, the gateway, RMP and Temporal.
STOPS = [*(unit for timer in TIMERS for unit in (timer, timer.replace(".timer", ".service"))),
         "rmp-code-watch", "openclaw-gateway", "rmp-api", "rmp-worker", "temporal"]
# What it starts again, in dependency order.
STARTS = ["temporal", "rmp-api", "rmp-worker", "openclaw-gateway", "rmp-code-watch", *TIMERS]

FAKE_CURL = r'''#!/usr/bin/env python3
"""curl, as far as the scripts use it against Qdrant's API. FAKE_QDRANT_* variables choose failures."""
import hashlib, io, json, os, sys, tarfile
args, method, out, url, form, i = sys.argv[1:], "GET", None, None, None, 0
while i < len(args):
    a = args[i]
    if a == "-X": method = args[i + 1]; i += 2; continue
    if a == "-o": out = args[i + 1]; i += 2; continue
    if a == "-F": form = args[i + 1]; i += 2; continue
    if a in ("-m", "--retry", "--retry-delay"): i += 2; continue
    if a.startswith("-"): i += 1; continue
    url = a; i += 1
state = os.environ["FAKE_STATE"]
with open(os.path.join(state, "curl.log"), "a") as log:
    log.write(f"{method} {url}\n")
env = os.environ.get
def snapshot_bytes():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in ("rmp_memories-5430049239505477-2026-10-08-14-56-36.snapshot", "config.json"):
            data = name.encode()
            info = tarfile.TarInfo(name); info.size = len(data); tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
if env("FAKE_QDRANT_DOWN") == "1":
    sys.exit(7)
if url.endswith("/readyz"):
    sys.exit(0)
if method == "GET" and url.endswith("/snapshots"):
    stale = [n for n in env("FAKE_QDRANT_STALE", "").split(",") if n]
    print(json.dumps({"result": [{"name": n, "size": 1} for n in stale]})); sys.exit(0)
if method == "POST" and url.endswith("/snapshots?wait=true"):
    if env("FAKE_QDRANT_CREATE_FAIL") == "1": sys.exit(22)
    data = snapshot_bytes()
    checksum = "0" * 64 if env("FAKE_QDRANT_BAD_CHECKSUM") == "1" else hashlib.sha256(data).hexdigest()
    print(json.dumps({"result": {"name": "full-snapshot-test.snapshot", "size": len(data), "checksum": checksum}}))
    sys.exit(0)
if method == "GET" and "/snapshots/" in url:
    if env("FAKE_QDRANT_DOWNLOAD_FAIL") == "1":
        open(out, "wb").write(b"partial"); sys.exit(18)
    open(out, "wb").write(snapshot_bytes()); sys.exit(0)
if method == "DELETE":
    if env("FAKE_QDRANT_DELETE_FAIL") == "1": sys.exit(22)
    print('{"result": true}'); sys.exit(0)
if method == "POST" and "/snapshots/upload" in url:
    if env("FAKE_QDRANT_UPLOAD_FAIL") == "1": sys.exit(22)
    with open(os.path.join(state, "uploads.log"), "a") as log:
        log.write(f"{url.split('/collections/')[1].split('/')[0]} {form}\n")
    print('{"result": true}'); sys.exit(0)
sys.exit(22)
'''

FAKE_PG_DUMP = r'''#!/bin/sh
printf '%s\n' "$*" >> "${FAKE_STATE}/pg_dump.log"
[ "${FAKE_PGDUMP_FAIL:-0}" = 1 ] && { echo "pg_dump: connection refused" >&2; exit 1; }
for a in "$@"; do last="$a"; done
while [ $# -gt 0 ]; do [ "$1" = "-f" ] && { printf 'PGDMP' > "$2"; exit 0; }; shift; done
printf 'PGDMP-%s' "$last"
'''

FAKE_SUDO = r'''#!/usr/bin/env python3
"""sudo -n -u <user> <command...>: records the user, the argv, the sha256 of stdin and whether PGPASSWORD is set in
${FAKE_STATE}/sudo.log, then runs the command as this user with the same stdin."""
import hashlib, json, os, subprocess, sys, time
args, user = sys.argv[1:], None
while args and args[0] in ("-n", "-u"):
    user, args = (args[1], args[2:]) if args[0] == "-u" else (user, args[1:])
data = sys.stdin.buffer.read()
with open(os.path.join(os.environ["FAKE_STATE"], "sudo.log"), "a") as log:
    log.write(json.dumps({"t": time.monotonic_ns(), "user": user, "argv": args,
                          "stdin_sha256": hashlib.sha256(data).hexdigest(),
                          "pgpassword": "PGPASSWORD" in os.environ}) + "\n")
sys.exit(subprocess.run(args, input=data).returncode)
'''

# pg_restore of an archive of the fake pg_dump on stdin: PGDMP is rmp_db (its -f form), PGDMP-<db> a Temporal one.
FAKE_PG_RESTORE = r'''#!/bin/sh
archive="$(cat)"
db="${archive#PGDMP-}"
[ "${archive}" = PGDMP ] && db=rmp_db
if [ "${FAKE_PGRESTORE_FAIL:-}" = "${db}" ]; then
  echo "pg_restore: error: could not execute query: database \"${db}\" is being accessed by other users" >&2
  exit 1
fi
exit 0
'''

# dropdb and createdb: the restore never calls them (the rmp role cannot create a database), so each call is recorded.
FAKE_REFUSED = r'''#!/bin/sh
printf '%s %s\n' "$(basename "$0")" "$*" >> "${FAKE_STATE}/refused.log"
exit 1
'''

FAKE_SLEEP = r'''#!/bin/sh
printf '%s\n' "$*" >> "${FAKE_STATE}/sleep.log"
'''

FAKE_SYSTEMCTL = r'''#!/usr/bin/env python3
"""systemctl, as far as ops/restore_backup.sh and ops/backup_openclaw_state.py use it.

Every call is recorded in ${FAKE_STATE}/systemctl.log, a start with the sha256 of the live settings at that moment.
Unit states live in ${FAKE_STATE}/units; stop and start update them. FAKE_STOP_FAIL=<unit> fails that unit's stop,
FAKE_LIST_UNITS_FAIL=1 every list-units.
FAKE_DEPLOY_ON_STOP=<unit>=<deploy unit> lists the deploy unit once the fake has handled that unit's stop.
FAKE_TIMER_RUN=<timer>=<n> keeps the timer's service active for n is-active calls after the timer starts.
"""
import hashlib, json, os, sys, time
args, env = sys.argv[1:], os.environ.get
state = env("FAKE_STATE")
units = os.path.join(state, "units")
words = [a for a in args if not a.startswith("-")]
verb = words[0]


def path(unit):
    return os.path.join(units, unit if "." in unit else unit + ".service")


def get(unit):
    return open(path(unit)).read() if os.path.exists(path(unit)) else "inactive"


def put(unit, value):
    with open(path(unit), "w") as f:
        f.write(value)


def service(timer):
    return timer[:-len(".timer")] + ".service"


record = {"t": time.monotonic_ns(), "argv": args}
if verb == "start":
    settings = env("RMP_SETTINGS_PATH", "")
    record["settings_sha256"] = (hashlib.sha256(open(settings, "rb").read()).hexdigest()
                                 if os.path.isfile(settings) else None)
with open(os.path.join(state, "systemctl.log"), "a") as log:
    log.write(json.dumps(record) + "\n")
timers = sorted(n for n in os.listdir(units) if n.endswith(".timer"))

if verb == "stop":
    for unit in words[1:]:
        if env("FAKE_STOP_FAIL") and path(env("FAKE_STOP_FAIL")) == path(unit):
            print(f"Job for {unit} failed.", file=sys.stderr)
            sys.exit(1)
        put(unit, "inactive")
        trigger, _, deploy = env("FAKE_DEPLOY_ON_STOP", "").partition("=")
        if trigger and path(trigger) == path(unit):
            with open(os.path.join(state, "deploy-units"), "a") as f:
                f.write(deploy + "\n")
elif verb == "start":
    for unit in words[1:]:
        put(unit, "active")
        timer, _, calls = env("FAKE_TIMER_RUN", "").partition("=")
        if unit == timer:
            put(service(timer), "active")
            with open(path(service(timer)) + ".run", "w") as f:
                f.write(calls)
elif verb == "is-active":
    current, run = get(words[1]), path(words[1]) + ".run"
    if os.path.exists(run):
        left = int(open(run).read()) - 1
        if left > 0:
            with open(run, "w") as f:
                f.write(str(left))
        else:
            os.remove(run)
            put(words[1], "inactive")
    if "--quiet" not in args:
        print(current)
    sys.exit(0 if current == "active" else 3)
elif verb == "list-units":
    if env("FAKE_LIST_UNITS_FAIL") == "1":
        print("Failed to connect to bus", file=sys.stderr)
        sys.exit(1)
    if "--type=timer" in args:
        listed = [t for t in timers if get(t) == "active"]
    else:
        deploys = os.path.join(state, "deploy-units")
        listed = open(deploys).read().split() if os.path.exists(deploys) else []
    for unit in listed:
        print(f"{unit} loaded active running {unit}")
elif verb == "show":
    print(service(words[-1]))
elif verb == "list-timers":
    for t in timers:
        armed = get(t) == "active" and get(service(t)) not in ("active", "activating")
        upcoming = "Tue 2026-10-20 03:15:00 UTC 5h left" if armed else "- -"
        print(f"{upcoming} Mon 2026-10-19 03:15:00 UTC 18h ago {t} {service(t)}")
'''

FAKES = {"curl": FAKE_CURL, "pg_dump": FAKE_PG_DUMP, "sudo": FAKE_SUDO, "pg_restore": FAKE_PG_RESTORE,
         "systemctl": FAKE_SYSTEMCTL, "sleep": FAKE_SLEEP, "dropdb": FAKE_REFUSED, "createdb": FAKE_REFUSED}
# The fakes' logs, which each call of host.run() starts without.
LOGS = ("systemctl.log", "sudo.log", "sleep.log", "refused.log")


def _store(path, rows):
    """A small SQLite store in the shape of OpenClaw's, holding ``rows``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS events (n INTEGER)")
        con.executemany("INSERT INTO events VALUES (?)", [(n,) for n in rows])
        con.commit()
    finally:
        con.close()


def _runs_only_against_fakes(script, env, fake):
    """The precondition of every script run here: each tool it could reach on the host resolves to its fake, and a
    restore takes every path it writes from the environment. A restore that hard-codes a production path is never
    executed: in a direct session it would remove the live artifacts and copy over the live settings."""
    if script.name == RESTORE.name:
        text = script.read_text()
        for name in RESTORE_PATHS:
            assert re.search(rf'^[A-Z_]+="\$\{{{name}:-', text, re.M), f"{script} hard-codes the path {name} sets"
    for tool in FAKES:
        assert shutil.which(tool, path=env["PATH"]) == str(fake / tool), f"{tool} does not resolve to its fake"


@pytest.fixture
def host(tmp_path):
    fake, state, data, home = tmp_path / "bin", tmp_path / "state", tmp_path / "data", tmp_path / "openclaw"
    for d in (fake, state, data, home, state / "units"):
        d.mkdir()
    for name, body in FAKES.items():
        (fake / name).write_text(body)
        (fake / name).chmod(0o755)
    for unit in (*TIMERS, "rmp-code-watch.service", "openclaw-gateway.service", "rmp-api.service", "rmp-worker.service",
                 "temporal.service", "rmp-qdrant.service"):
        (state / "units" / unit).write_text("active")
    stores = {"openclaw-agent.sqlite": home / "agents" / "main" / "agent" / "openclaw-agent.sqlite",
              "openclaw.sqlite": home / "state" / "openclaw.sqlite"}
    for rows, store in enumerate(stores.values(), start=2):
        _store(store, range(rows))
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"vector_memory": {"qdrant_mode": "server", "qdrant_host": "127.0.0.1", "qdrant_port": 6333}}))
    env = {**os.environ, "RMP_ROOT": str(REPO), "RMP_DATA_DIR": str(data), "RMP_BACKUP_ROOT": str(data / "backups"),
           "RMP_PYTHON": sys.executable, "RMP_SETTINGS_PATH": str(settings), "OPENCLAW_HOME": str(home),
           "RMP_CODE_RELOAD_LOCK": str(tmp_path / "code-reload.lock"),
           "PATH": f"{fake}:{os.environ['PATH']}", "FAKE_STATE": str(state)}
    for name in ("PGPASSWORD", "RMP_PG_SOCKET_DIR", "RMP_PG_PORT"):
        env.pop(name, None)

    def run(script=BACKUP, *args, answer="y\n", **extra):
        _runs_only_against_fakes(script, {**env, **extra}, fake)
        for name in LOGS:
            (state / name).unlink(missing_ok=True)
        result = subprocess.run(["bash", str(script), *args], env={**env, **extra}, cwd=tmp_path, input=answer,
                                capture_output=True, text=True, timeout=300)
        backups = data / "backups"
        stamps = sorted(p for p in backups.iterdir() if p.name[:2] == "20") if backups.is_dir() else []
        dest = stamps[-1] if stamps else None
        manifest = json.loads((dest / "manifest.json").read_text()) if dest and (dest / "manifest.json").exists() else None
        log = state / "curl.log"
        calls = [line.split(" ", 1) for line in log.read_text().splitlines()] if log.exists() else []
        log.unlink(missing_ok=True)
        return result, dest, manifest, calls

    def restore(src, answer="y\n", **extra):
        return run(RESTORE, str(src), answer=answer, **extra)[0]

    def log(name):
        path = state / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    return type("Host", (), {"run": staticmethod(run), "restore": staticmethod(restore), "log": staticmethod(log),
                             "tmp": tmp_path, "state": state, "backups": data / "backups", "settings": settings,
                             "stores": stores})


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_a_healthy_backup_takes_a_verified_qdrant_snapshot_and_dumps_temporal(host):
    result, dest, manifest, calls = host.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert manifest["failures"] == []
    assert (dest / "rmp_db.dump").read_bytes() == b"PGDMP"
    assert (dest / "temporal.dump").read_bytes() == b"PGDMP-temporal"
    assert (dest / "temporal_visibility.dump").read_bytes() == b"PGDMP-temporal_visibility"
    assert tarfile.open(dest / "qdrant-full.snapshot").getnames()[1] == "config.json"
    assert not list(dest.glob("*.partial")) and not (dest / "qdrant.tar.gz").exists()
    assert [m for m, _ in calls] == ["GET", "GET", "POST", "GET", "DELETE"]
    assert calls[0][1].endswith("/readyz") and calls[2][1].endswith("/snapshots?wait=true")
    assert calls[4][1].endswith("/snapshots/full-snapshot-test.snapshot")
    assert oct((dest / "rmp_db.dump").stat().st_mode & 0o777) == "0o600"


def test_the_script_does_not_depend_on_its_working_directory(host):
    """rmp-backup.service starts it with no working directory: the run above already happens from tmp, not the repo."""
    result, _, manifest, _ = host.run()
    assert result.returncode == 0 and manifest["failures"] == []


@pytest.mark.parametrize("failure, words", [
    ("FAKE_QDRANT_CREATE_FAIL", "the full snapshot was not created"),
    ("FAKE_QDRANT_DOWNLOAD_FAIL", "the download of full-snapshot-test.snapshot failed"),
    ("FAKE_QDRANT_BAD_CHECKSUM", "does not match Qdrant's checksum"),
    ("FAKE_QDRANT_DOWN", "is not ready"),
])
def test_a_failed_qdrant_snapshot_fails_the_run_leaves_no_partial_file_and_still_cleans_up_the_server(host, failure, words):
    result, dest, manifest, calls = host.run(**{failure: "1"})
    assert result.returncode == 1, result.stdout
    assert manifest["failures"] == ["qdrant"] and words in result.stdout
    assert not (dest / "qdrant-full.snapshot").exists() and not list(dest.glob("*.partial"))
    assert (dest / "rmp_db.dump").exists() and (dest / "openclaw-state").exists()
    if failure in ("FAKE_QDRANT_DOWNLOAD_FAIL", "FAKE_QDRANT_BAD_CHECKSUM"):
        assert calls[-1] == ["DELETE", calls[-1][1]] and calls[-1][1].endswith("full-snapshot-test.snapshot")


def test_snapshots_left_by_a_killed_run_are_removed_first_and_a_failed_delete_is_only_a_warning(host):
    result, _, manifest, calls = host.run(FAKE_QDRANT_STALE="full-snapshot-old.snapshot")
    assert result.returncode == 0 and manifest["failures"] == []
    assert ["DELETE", calls[2][1]] == calls[2] and calls[2][1].endswith("/snapshots/full-snapshot-old.snapshot")
    result, _, manifest, _ = host.run(FAKE_QDRANT_DELETE_FAIL="1")
    assert result.returncode == 0 and "could not delete Qdrant snapshot" in result.stdout


def test_a_failed_postgres_dump_is_reported_not_swallowed(host):
    result, dest, manifest, _ = host.run(FAKE_PGDUMP_FAIL="1")
    assert result.returncode == 1 and manifest["failures"] == ["postgres", "temporal:temporal", "temporal:temporal_visibility"]
    assert (dest / "qdrant-full.snapshot").exists()


def test_every_dump_bounds_its_lock_wait(host):
    """A dump that cannot take its table locks within 5 minutes fails instead of hanging the night's run."""
    result, _, manifest, _ = host.run()
    assert result.returncode == 0 and manifest["failures"] == []
    dumps = [line.split() for line in (host.state / "pg_dump.log").read_text().splitlines()]
    assert len(dumps) == 3 and all("--lock-wait-timeout=5min" in args for args in dumps)
    assert "rmp_db" in dumps[0] and dumps[1][-1] == "temporal" and dumps[2][-1] == "temporal_visibility"


def test_embedded_mode_archives_the_storage_directory(host):
    storage = host.tmp / "qdrant-embedded"
    (storage / "collection").mkdir(parents=True)
    (storage / "collection" / "segment").write_text("x")
    host.settings.write_text(json.dumps({"vector_memory": {"qdrant_mode": "embedded", "qdrant_host": "",
                                                           "qdrant_path": str(storage)}}))
    result, dest, manifest, calls = host.run()
    assert result.returncode == 0 and manifest["failures"] == [], result.stdout
    assert "qdrant-embedded/collection/segment" in tarfile.open(dest / "qdrant.tar.gz").getnames()
    assert calls == []


def _dated(root, days, old, failures=(), month="09"):
    """Dated backups as ops/backup.sh leaves them; ``failures`` marks them failed (unfinished if None)."""
    for day in days:
        stamp = root / f"2026{month}{day:02d}T031500Z"
        stamp.mkdir(parents=True)
        if failures is not None:
            (stamp / "manifest.json").write_text(json.dumps({"failures": list(failures)}))
        os.utime(stamp, (old + day * 3600, old + day * 3600))


def test_pruning_keeps_fourteen_dated_backups_and_never_touches_other_directories(host):
    old = time.time() - 30 * 86400
    keep = host.backups / "openclaw-update-20260930T222604Z"
    keep.mkdir(parents=True)
    os.utime(keep, (old - 86400, old - 86400))
    _dated(host.backups, range(1, 17), old)
    result, _, _, _ = host.run()
    assert result.returncode == 0, result.stdout + result.stderr
    dated = sorted(p.name for p in host.backups.iterdir() if p.name[:2] == "20")
    assert len(dated) == 14 and "20260903T031500Z" not in dated and "20260904T031500Z" in dated  # 16 old + 1 new
    assert keep.is_dir(), "a non-dated directory is never pruned by age"


def test_failed_backups_never_count_toward_the_fourteen_kept(host):
    """Five failed nights must not cost five good backups: the first good run prunes only the oldest complete one."""
    old = time.time() - 40 * 86400
    _dated(host.backups, range(1, 15), old)                                # 14 complete
    _dated(host.backups, range(15, 20), old, failures=["qdrant"])           # 5 newer failed
    _dated(host.backups, range(1, 3), old - 10 * 86400, failures=["qdrant"], month="08")  # 2 failed, oldest of all
    _dated(host.backups, range(3, 4), old - 10 * 86400, failures=None, month="08")        # 1 unfinished (no manifest)
    result, _, manifest, _ = host.run()
    assert result.returncode == 0 and manifest["failures"] == [], result.stdout
    names = {p.name for p in host.backups.iterdir() if p.name[:2] == "20"}
    assert "20260901T031500Z" not in names and "20260902T031500Z" in names    # 14 newest complete kept, incl. today's
    assert {f"202609{d:02d}T031500Z" for d in range(15, 20)} <= names         # failed but newer than the oldest kept
    assert not any(n.startswith("202608") for n in names)                     # failed/unfinished older than it: gone


def test_a_failed_run_prunes_nothing(host):
    _dated(host.backups, range(1, 17), time.time() - 30 * 86400)
    result, _, _, _ = host.run(FAKE_QDRANT_CREATE_FAIL="1")
    assert result.returncode == 1 and "no older backup was pruned" in result.stdout
    assert len([p for p in host.backups.iterdir() if p.name[:2] == "20"]) == 17


def test_a_second_run_while_one_holds_the_lock_exits_without_touching_anything(host):
    import fcntl
    host.backups.mkdir(parents=True)
    with open(host.backups / ".backup.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result, dest, _, calls = host.run()
    assert result.returncode == 1 and "another backup is running" in result.stderr
    assert dest is None and calls == []


def test_restore_uploads_every_collection_of_a_full_snapshot(host):
    _, dest, _, _ = host.run()
    result, _, _, calls = host.run(RESTORE_QDRANT, str(dest / "qdrant-full.snapshot"), "http://127.0.0.1:6333")
    assert result.returncode == 0, result.stdout + result.stderr
    uploads = (host.state / "uploads.log").read_text().splitlines()
    assert len(uploads) == 1 and uploads[0].startswith("rmp_memories snapshot=@")
    assert [m for m, _ in calls] == ["GET", "POST"]
    assert calls[1][1].endswith("/collections/rmp_memories/snapshots/upload?priority=snapshot&wait=true")


def test_restore_fails_loudly_when_an_upload_fails_or_the_file_is_not_a_snapshot(host):
    _, dest, _, _ = host.run()
    result, _, _, _ = host.run(RESTORE_QDRANT, str(dest / "qdrant-full.snapshot"), "http://127.0.0.1:6333",
                               FAKE_QDRANT_UPLOAD_FAIL="1")
    assert result.returncode == 1 and "not restored: rmp_memories" in result.stderr
    junk = host.tmp / "junk.snapshot"
    junk.write_text("not a tar")
    result, _, _, _ = host.run(RESTORE_QDRANT, str(junk), "http://127.0.0.1:6333")
    assert result.returncode == 1 and "not a readable archive" in result.stderr


def test_restore_recreates_each_database_from_its_dump_through_the_postgres_superuser(host):
    """The rmp role has neither CREATEDB nor superuser: dropdb and createdb as rmp would leave rmp_db dropped."""
    _, dest, _, _ = host.run()
    result = host.restore(dest)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = host.log("sudo.log")
    assert [(c["user"], c["argv"]) for c in calls] == [("postgres", PG_RESTORE)] * 3
    assert [c["stdin_sha256"] for c in calls] == [
        _sha256(dest / name) for name in ("rmp_db.dump", "temporal.dump", "temporal_visibility.dump")]
    assert not any(c["pgpassword"] or "-U" in c["argv"] for c in calls)
    assert not (host.state / "refused.log").exists(), "dropdb or createdb was called"


def test_restore_pg_database_refuses_a_missing_dump_and_reports_a_failed_restore(host):
    empty = host.tmp / "empty.dump"
    empty.touch()
    for dump in (empty, host.tmp / "missing.dump"):
        result, _, _, _ = host.run(RESTORE_PG, str(dump))
        assert result.returncode == 1 and f"{dump} is missing or empty" in result.stderr
        assert host.log("sudo.log") == []
    _, dest, _, _ = host.run()
    result = host.restore(dest, FAKE_PGRESTORE_FAIL="rmp_db")
    assert result.returncode == 1 and "Restore INCOMPLETE: not restored: postgres\n" in result.stderr
    assert len(host.log("sudo.log")) == 3


def _in_order(host):
    """systemctl's and sudo's calls, merged in the order they were made, as (tool, argv, record)."""
    calls = [(record["t"], tool, record) for tool in ("systemctl", "sudo") for record in host.log(f"{tool}.log")]
    return [(tool, record["argv"], record) for _, tool, record in sorted(calls, key=lambda call: call[0])]


def _verbs(host, verb):
    return [record["argv"][1] for record in host.log("systemctl.log") if record["argv"][0] == verb]


def test_restore_stops_every_writer_before_the_first_database_and_starts_them_after_the_last_step(host):
    _, dest, _, _ = host.run()
    host.settings.write_text(json.dumps({**json.loads(host.settings.read_text()), "changed": "after the backup"}))
    assert _sha256(host.settings) != _sha256(dest / "settings.json")
    result = host.restore(dest)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _in_order(host)
    databases = [i for i, (tool, _, _) in enumerate(calls) if tool == "sudo"]
    assert len(databases) == 3
    assert [argv[1] for _, argv, _ in calls[:databases[0]] if argv[0] == "stop"] == STOPS
    assert [argv[1] for _, argv, _ in calls[databases[0]:] if argv[0] == "stop"] == []
    starts = [(i, argv[1], record["settings_sha256"]) for i, (_, argv, record) in enumerate(calls) if argv[0] == "start"]
    assert [unit for _, unit, _ in starts] == STARTS
    assert all(i > databases[-1] for i, _, _ in starts)
    # Every start saw the backup's settings in place: the starts follow the settings copy.
    assert {sha for _, _, sha in starts} == {_sha256(dest / "settings.json")}
    assert calls[-1][1][0] == "list-timers"
    assert result.stdout.count("\nnext: ") == len(TIMERS) and "NO NEXT" not in result.stdout
    assert result.stdout.rstrip().endswith("Restore complete.")


def test_a_restore_that_fails_after_its_first_database_step_starts_nothing(host):
    """rmp-api runs create_all on rmp_db at start, and the watchdog would restart Temporal on a partial database."""
    _, dest, _, _ = host.run()
    result = host.restore(dest, FAKE_PGRESTORE_FAIL="temporal")
    assert result.returncode == 1
    assert _verbs(host, "stop") == STOPS and _verbs(host, "start") == []
    units = " ".join(STARTS)
    assert "Restore INCOMPLETE: not restored: temporal:temporal\n" in result.stderr
    assert f"Left stopped, because a store may be half restored: {units}\n" in result.stderr
    assert f"When the stores are whole, start them with: systemctl start {units}\n" in result.stderr
    assert ("Do not start rmp-api, rmp-worker or openclaw-gateway, run a deploy or reboot before the stores are whole: "
            "each starts rmp-api, which runs create_all on rmp_db\n") in result.stderr


def test_a_restore_refused_by_the_code_reload_lock_stops_nothing(host):
    _, dest, _, _ = host.run()
    lock = host.tmp / "code-reload.lock"
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = host.restore(dest)
    assert result.returncode == 1
    assert f"refused: a deploy or code reload holds {lock}; run the restore again when it ends" in result.stderr
    assert _verbs(host, "stop") == [] and host.log("sudo.log") == []


def test_a_declined_restore_touches_nothing(host):
    _, dest, _, _ = host.run()
    result = host.restore(dest, answer="n\n")
    assert result.returncode == 0
    assert host.log("systemctl.log") == [] and host.log("sudo.log") == []
    assert not (host.tmp / "code-reload.lock").exists()


def test_a_restore_refuses_while_a_deploy_unit_is_active(host):
    """A deploy blocks on the lock, then restarts rmp-api and rmp-worker: it must not outlive the restore."""
    _, dest, _, _ = host.run()
    (host.state / "deploy-units").write_text("aura-deploy-main.service\n")
    result = host.restore(dest)
    assert result.returncode == 1
    assert "refused: aura-deploy-main.service is active; run the restore again when it ends" in result.stderr
    assert _verbs(host, "stop") == []
    (host.state / "deploy-units").unlink()
    result = host.restore(dest, FAKE_LIST_UNITS_FAIL="1")
    assert result.returncode == 1 and "refused: systemctl could not list the deploy units" in result.stderr
    assert _verbs(host, "stop") == []
    # Launched by rmp-api or rmp-worker after the first check: the second check, after the last stop, finds it.
    result = host.restore(dest, FAKE_DEPLOY_ON_STOP="temporal=aura-deploy-main.service")
    assert result.returncode == 1
    assert ("refused: aura-deploy-main.service started while the restore stopped the services; "
            "run the restore again when it ends") in result.stderr
    assert host.log("sudo.log") == []
    assert _verbs(host, "stop") == STOPS and _verbs(host, "start") == STARTS
    assert host.log("systemctl.log")[-1]["argv"][0] == "list-timers"


def test_a_failed_stop_ends_the_restore_and_starts_what_it_stopped(host):
    _, dest, _, _ = host.run()
    result = host.restore(dest, FAKE_STOP_FAIL="rmp-worker")
    assert result.returncode == 1 and "systemctl stop rmp-worker failed: nothing was restored" in result.stderr
    assert host.log("sudo.log") == []
    assert _verbs(host, "start") == [unit for unit in STARTS if unit not in ("rmp-worker", "temporal")]


def test_a_timer_whose_run_is_in_progress_after_the_restart_is_waited_for(host):
    """A started timer whose interval passed during the restore runs its service at once and shows no next elapse
    until that run ends."""
    _, dest, _, _ = host.run()
    result = host.restore(dest, FAKE_TIMER_RUN="rmp-temporal-watchdog.timer=3")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "NO NEXT:" not in result.stdout + result.stderr
    assert (host.state / "sleep.log").read_text().splitlines() == ["10"] * 3
    result = host.restore(dest, FAKE_TIMER_RUN="rmp-temporal-watchdog.timer=100")
    assert result.returncode == 1
    assert ("NO NEXT: rmp-temporal-watchdog.timer (rmp-temporal-watchdog.service still running after 15 min)\n"
            in result.stdout)
    assert (host.state / "sleep.log").read_text().splitlines() == ["10"] * 90


def test_restore_puts_the_openclaw_stores_back_with_the_gateway_stopped(host):
    """Sessions, transcripts, auth profiles and cron jobs are coupled to rmp_db: they come back with it."""
    _, dest, manifest, _ = host.run()
    assert manifest["failures"] == []
    for name, live in host.stores.items():
        _store(live, range(100, 110))
        assert _sha256(live) != _sha256(dest / "openclaw-state" / name)
    result = host.restore(dest)
    assert result.returncode == 0, result.stdout + result.stderr
    for name, live in host.stores.items():
        assert _sha256(live) == _sha256(dest / "openclaw-state" / name)
    calls = [record["argv"] for record in host.log("systemctl.log")]
    # The tool refuses while the gateway runs; it asked only after the restore had stopped it.
    assert calls.index(["stop", "openclaw-gateway"]) < calls.index(["is-active", "--quiet", "openclaw-gateway"])
    assert "restored agent, state from" in result.stdout

@pytest.mark.parametrize("name", RESTORE_PATHS)
def test_a_restore_that_hard_codes_a_production_path_is_never_run(host, name):
    script = host.tmp / RESTORE.name
    script.write_text(RESTORE.read_text().replace(f"${{{name}:-", f"${{UNREAD_{name}:-"))
    assert script.read_text() != RESTORE.read_text()
    with pytest.raises(AssertionError, match=f"hard-codes the path {name} sets"):
        host.run(script, str(host.tmp))
    assert not (host.state / "systemctl.log").exists()
