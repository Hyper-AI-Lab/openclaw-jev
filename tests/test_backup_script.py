"""ops/backup.sh and the restore scripts end to end, against fake pg_dump, pg_restore, sudo, systemctl, restic and
Qdrant API.

The scripts run from outside the checkout (as rmp-backup.service does, with no working directory), with their data,
settings and OpenClaw home in the test's tmp tree, so nothing live is read. A script runs only when every tool it
could reach on the host resolves to its fake, and a restore only when it takes every path it writes from the
environment: the production guard does not see a child process's own calls. One test runs the real restic, against a
repository in its tmp tree.
"""
import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import socket
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
QDRANT_CURL = REPO / "ops" / "qdrant_curl.sh"
# Every path the restore writes or locks comes from one of these, each with a default (the names of ops/backup.sh and
# of the code reload's lock).
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
# A dummy Qdrant API key: a script run with it must send it on curl's stdin and never show it.
QDRANT_KEY = "dummy-qdrant-key-" + secrets.token_hex(8)
# The host's restic, found before any test puts its fakes on a PATH; None where it is not installed, as in CI.
RESTIC = shutil.which("restic")

FAKE_CURL = r'''#!/usr/bin/env python3
"""curl, as far as the scripts use it against Qdrant's API. FAKE_QDRANT_* variables choose failures. Each call's argv
and the headers of a config read from stdin (-K -) are recorded in ${FAKE_STATE}/curl-argv.log."""
import hashlib, io, json, os, re, sys, tarfile
args, method, out, url, form, config, i = sys.argv[1:], "GET", None, None, None, None, 0
while i < len(args):
    a = args[i]
    if a == "-X": method = args[i + 1]; i += 2; continue
    if a == "-o": out = args[i + 1]; i += 2; continue
    if a == "-F": form = args[i + 1]; i += 2; continue
    if a == "-K": config = args[i + 1]; i += 2; continue
    if a in ("-m", "--retry", "--retry-delay"): i += 2; continue
    if a.startswith("-"): i += 1; continue
    url = a; i += 1
headers = re.findall(r'^header = "(.*)"$', sys.stdin.read(), re.M) if config == "-" else []
state = os.environ["FAKE_STATE"]
with open(os.path.join(state, "curl.log"), "a") as log:
    log.write(f"{method} {url}\n")
with open(os.path.join(state, "curl-argv.log"), "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "headers": headers}) + "\n")
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

# pg_restore of an archive of the fake pg_dump: PGDMP is rmp_db (its -f form), PGDMP-<db> a Temporal one. A restore
# reads it on stdin; the backup's read-back, pg_restore -f /dev/null <dump>, names it, and is recorded in
# ${FAKE_STATE}/pg_restore.log. FAKE_PGRESTORE_FAIL=<db> fails either for that database.
# FAKE_PGRESTORE_SIGNAL=<db>=<signal> sends the signal to the restore_backup.sh this call descends from while it
# restores <db>, as a hung-up SSH session or a kill would.
FAKE_PG_RESTORE = r'''#!/bin/sh
if [ "$1" = "-f" ]; then
  printf '%s\n' "$*" >> "${FAKE_STATE}/pg_restore.log"
  archive="$(cat "$3")"
else
  archive="$(cat)"
fi
db="${archive#PGDMP-}"
[ "${archive}" = PGDMP ] && db=rmp_db
if [ "${FAKE_PGRESTORE_SIGNAL%%=*}" = "${db}" ]; then
  pid=$$
  while [ "${pid}" -gt 1 ] && ! grep -q 'restore_backup\.sh' "/proc/${pid}/cmdline"; do
    pid="$(sed 's/.*) //' "/proc/${pid}/stat" | cut -d' ' -f2)"
  done
  [ "${pid}" -gt 1 ] && kill -s "${FAKE_PGRESTORE_SIGNAL#*=}" "${pid}"
fi
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

# openclaw --version, which ops/backup_openclaw_state.py records in its manifest.
FAKE_OPENCLAW = r'''#!/bin/sh
echo "OpenClaw 2026.9.7"
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
FAKE_SIGNAL_ON_STOP=<unit>=<signal> sends the signal to the restore, the fake's parent, once it has stopped that unit;
FAKE_SIGNAL_ON_START=<unit>=<signal> once it has started it. A '+self' suffix (<signal>+self) then ends the fake
with the same signal, as a real hang-up does to the whole foreground job, so bash reports the killed child.
FAKE_TIMER_RUN=<timer>=<n> keeps the timer's service active for n is-active calls after the timer starts, or in the
state FAKE_TIMER_RUN_STATE names: activating is how a Type=oneshot service shows its run.
"""
import hashlib, json, os, signal, sys, time
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


def signal_restore(variable, unit):
    trigger, _, name = env(variable, "").partition("=")
    if trigger and path(trigger) == path(unit):
        name, _, itself = name.partition("+")
        os.kill(os.getppid(), getattr(signal, "SIG" + name))
        if itself == "self":
            os.kill(os.getpid(), getattr(signal, "SIG" + name))


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
        signal_restore("FAKE_SIGNAL_ON_STOP", unit)
elif verb == "start":
    for unit in words[1:]:
        put(unit, "active")
        timer, _, calls = env("FAKE_TIMER_RUN", "").partition("=")
        if unit == timer:
            put(service(timer), env("FAKE_TIMER_RUN_STATE", "active"))
            with open(path(service(timer)) + ".run", "w") as f:
                f.write(calls)
        signal_restore("FAKE_SIGNAL_ON_START", unit)
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

FAKE_RESTIC = r'''#!/usr/bin/env python3
"""restic, as ops/offsite_copy.sh runs it: each call's argv, and the RESTIC_* and AWS_* variables it got, are recorded
in ${FAKE_STATE}/restic.log. FAKE_RESTIC_FAIL=<command> fails that command, as an unreachable repository would."""
import json, os, sys
with open(os.path.join(os.environ["FAKE_STATE"], "restic.log"), "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "env": {name: value for name, value in os.environ.items()
                                                         if name.startswith(("RESTIC_", "AWS_"))}}) + "\n")
if os.environ.get("FAKE_RESTIC_FAIL") == sys.argv[1]:
    print("Fatal: unable to open config file: Stat: connection refused", file=sys.stderr)
    sys.exit(1)
'''

FAKES = {"curl": FAKE_CURL, "pg_dump": FAKE_PG_DUMP, "sudo": FAKE_SUDO, "pg_restore": FAKE_PG_RESTORE,
         "systemctl": FAKE_SYSTEMCTL, "sleep": FAKE_SLEEP, "dropdb": FAKE_REFUSED, "createdb": FAKE_REFUSED,
         "openclaw": FAKE_OPENCLAW, "restic": FAKE_RESTIC}
# The fakes' logs, which each call of host.run() starts without.
LOGS = ("systemctl.log", "sudo.log", "sleep.log", "refused.log", "curl-argv.log", "pg_restore.log", "restic.log")


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
    executed: in a direct session it would remove the live artifacts and copy over the live settings. Every Qdrant call
    goes through ops/qdrant_curl.sh, which runs the checkout and the interpreter the environment names and reads the
    key from /etc/rmp/rmp.env unless QDRANT_API_KEY is set: a script runs only with the variable set, or with an
    interpreter that is one of the test's fakes."""
    if script.name == RESTORE.name:
        text = script.read_text()
        for name in RESTORE_PATHS:
            assert re.search(rf'^[A-Z_]+="\$\{{{name}:-', text, re.M), f"{script} hard-codes the path {name} sets"
    assert "QDRANT_API_KEY" in env or Path(env["RMP_PYTHON"]).parent == fake, \
        "without QDRANT_API_KEY, ops/qdrant_curl.sh reads the key from /etc/rmp/rmp.env"
    for path, name in ((RESTORE_QDRANT, "RMP_ROOT"), (QDRANT_CURL, "RMP_ROOT"), (QDRANT_CURL, "RMP_PYTHON")):
        assert re.search(rf'^[A-Z_]+="\$\{{{name}:-', path.read_text(), re.M), f"{path} hard-codes {name}"
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
    (home / "openclaw.json").write_text("{}")
    (home / "workspace").mkdir()
    (home / "workspace" / "AGENTS.md").write_text("# Aura\n")
    (data / "artifacts").mkdir()
    (data / "artifacts" / "report.json").write_text("{}")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"vector_memory": {"qdrant_mode": "server", "qdrant_host": "127.0.0.1", "qdrant_port": 6333}}))
    # No Qdrant key unless a test gives one, whatever the suite inherited.
    env = {**os.environ, "RMP_ROOT": str(REPO), "RMP_DATA_DIR": str(data), "RMP_BACKUP_ROOT": str(data / "backups"),
           "RMP_PYTHON": sys.executable, "RMP_SETTINGS_PATH": str(settings), "OPENCLAW_HOME": str(home),
           "RMP_CODE_RELOAD_LOCK": str(tmp_path / "code-reload.lock"), "QDRANT_API_KEY": "",
           "PATH": f"{fake}:{os.environ['PATH']}", "FAKE_STATE": str(state)}
    for name in ("PGPASSWORD", "RMP_PG_SOCKET_DIR", "RMP_PG_PORT"):
        env.pop(name, None)

    def prepared(script, extra):
        """The environment of a run of ``script``, once the precondition holds; the fakes' logs start empty. A
        variable given as None is left out."""
        run_env = {name: value for name, value in {**env, **extra}.items() if value is not None}
        _runs_only_against_fakes(script, run_env, fake)
        for name in LOGS:
            (state / name).unlink(missing_ok=True)
        return run_env

    def run(script=BACKUP, *args, answer="y\n", cwd=None, **extra):
        result = subprocess.run(["bash", str(script), *args], env=prepared(script, extra), cwd=cwd or tmp_path,
                                input=answer, capture_output=True, text=True, timeout=300)
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

    def spawn(src, output, **extra):
        """The restore running, its stdin a pipe the test writes, its stdout and stderr both going to ``output``."""
        return subprocess.Popen(["bash", str(RESTORE), str(src)], env=prepared(RESTORE, extra), cwd=tmp_path,
                                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT)

    def log(name):
        path = state / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    return type("Host", (), {"run": staticmethod(run), "restore": staticmethod(restore), "spawn": staticmethod(spawn),
                             "log": staticmethod(log),
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
    assert tarfile.open(dest / "artifacts.tar.gz").getnames() == ["artifacts", "artifacts/report.json"]
    assert not list(dest.glob("*.partial")) and not (dest / "qdrant.tar.gz").exists()
    assert [m for m, _ in calls] == ["GET", "GET", "POST", "GET", "DELETE"]
    assert calls[0][1].endswith("/readyz") and calls[2][1].endswith("/snapshots?wait=true")
    assert calls[4][1].endswith("/snapshots/full-snapshot-test.snapshot")
    assert oct((dest / "rmp_db.dump").stat().st_mode & 0o777) == "0o600"


def test_a_healthy_backup_writes_a_manifest_that_lists_every_file(host):
    result, dest, manifest, _ = host.run()
    assert result.returncode == 0 and manifest["failures"] == [], result.stdout + result.stderr
    assert {key: manifest[key] for key in ("format", "timestamp", "qdrant_mode")} == {
        "format": 2, "timestamp": dest.name, "qdrant_mode": "server"}
    assert manifest["components"] == {
        "postgres": ["rmp_db.dump"], "temporal": ["temporal.dump", "temporal_visibility.dump"],
        "qdrant": ["qdrant-full.snapshot"], "artifacts": ["artifacts.tar.gz"],
        "openclaw-state": ["openclaw-state/manifest.json", "openclaw-state/openclaw-agent.sqlite",
                           "openclaw-state/openclaw.sqlite"],
        "workspace": ["workspace.tar.gz"], "settings": ["settings.json", "openclaw.json"]}
    listed = [name for names in manifest["components"].values() for name in names]
    assert manifest["files"] == {name: {"bytes": (dest / name).stat().st_size, "sha256": _sha256(dest / name)}
                                 for name in listed}
    # Every file the run made is listed, but its log and the manifest itself.
    assert {str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()} == {*listed, "backup.log",
                                                                                  "manifest.json"}
    assert oct((dest / "manifest.json").stat().st_mode & 0o777) == "0o600"
    # Each dump read to its end before the manifest was written.
    assert (host.state / "pg_restore.log").read_text().splitlines() == [
        f"-f /dev/null {dest / name}" for name in ("rmp_db.dump", "temporal.dump", "temporal_visibility.dump")]
    # The check the programme runs on the next nightly backup.
    verify = subprocess.run([sys.executable, "-m", "app.production.backup_manifest", "verify", str(dest)], cwd=REPO,
                            capture_output=True, text=True, timeout=120)
    assert verify.returncode == 0 and verify.stdout == "" and verify.stderr == ""


def test_a_missing_component_fails_the_run_instead_of_being_skipped(host):
    """A component that silently made nothing, such as a missing openclaw.json or an OpenClaw store that moved, fails
    the run: exit 1, and no pruning."""
    config = host.tmp / "openclaw" / "openclaw.json"
    config.unlink()
    result, dest, manifest, _ = host.run()
    assert result.returncode == 1 and manifest["failures"] == ["settings"]
    assert f"FAIL: settings: could not copy {config}" in result.stdout
    assert "openclaw.json" in manifest["components"]["settings"] and "openclaw.json" not in manifest["files"]
    assert "Backup INCOMPLETE: settings failed" in result.stdout
    config.write_text("{}")
    host.stores["openclaw-agent.sqlite"].unlink()
    shutil.rmtree(dest)  # a second run within the same second would reuse its directory
    result, dest, manifest, _ = host.run()
    assert result.returncode == 1 and manifest["failures"] == ["openclaw-state"]
    assert "FAIL: openclaw-state: openclaw-state/manifest.json does not list agent" in result.stderr
    assert "FAIL: openclaw-state: openclaw-state/manifest.json does not list agent" in (dest / "backup.log").read_text()


def test_a_dump_that_does_not_read_back_fails_the_backup_run(host):
    result, dest, manifest, _ = host.run(FAKE_PGRESTORE_FAIL="temporal")
    assert result.returncode == 1 and manifest["failures"] == ["temporal:temporal"]
    assert "FAIL: temporal:temporal: temporal.dump does not read back" in result.stderr
    assert manifest["files"]["temporal.dump"]["bytes"] == (dest / "temporal.dump").stat().st_size


def test_the_script_does_not_depend_on_its_working_directory(host):
    """rmp-backup.service has no WorkingDirectory=, so systemd starts the script from /: its app imports resolve only
    because it changes to the RMP root first."""
    result, _, manifest, _ = host.run(cwd="/")
    assert result.returncode == 0 and manifest["failures"] == [], result.stdout + result.stderr


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


def test_every_qdrant_call_of_a_backup_sends_the_key_on_stdin_only(host):
    """With a key, each call to Qdrant's API (readyz, the stale snapshots' listing and removal, the snapshot, its
    download and its removal) sends it in the api-key header read from curl's stdin. No command line holds it, and
    neither does the output, the log or any file of the backup. Without a key the calls are plain curl."""
    result, dest, manifest, calls = host.run(QDRANT_API_KEY=QDRANT_KEY, FAKE_QDRANT_STALE="full-snapshot-old.snapshot")
    assert result.returncode == 0 and manifest["failures"] == [], result.stdout + result.stderr
    assert [m for m, _ in calls] == ["GET", "GET", "DELETE", "POST", "GET", "DELETE"]
    sent = host.log("curl-argv.log")
    assert len(sent) == 6 and all(call["argv"][:2] == ["-K", "-"] for call in sent)
    assert all(call["headers"] == [f"api-key: {QDRANT_KEY}"] for call in sent)
    assert not any(QDRANT_KEY in word for call in sent for word in call["argv"])
    assert QDRANT_KEY not in result.stdout + result.stderr
    assert not [p for p in dest.rglob("*") if p.is_file() and QDRANT_KEY.encode() in p.read_bytes()]
    shutil.rmtree(dest)  # a second run within the same second would reuse its directory
    result, _, manifest, calls = host.run(QDRANT_API_KEY="")
    assert result.returncode == 0 and manifest["failures"] == [], result.stdout + result.stderr
    sent = host.log("curl-argv.log")
    assert len(sent) == len(calls) == 5 and not any("-K" in call["argv"] or call["headers"] for call in sent)


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


def test_the_manifest_expects_a_dump_of_each_temporal_database_the_run_was_set_to_dump(host):
    result, dest, manifest, _ = host.run(RMP_TEMPORAL_DBS="temporal")
    assert result.returncode == 0 and manifest["failures"] == [], result.stdout + result.stderr
    assert manifest["components"]["temporal"] == ["temporal.dump"] and not (dest / "temporal_visibility.dump").exists()
    # A run set to dump none has no Temporal backup: that fails the component.
    shutil.rmtree(dest)  # a second run within the same second would reuse its directory
    result, dest, manifest, _ = host.run(RMP_TEMPORAL_DBS="")
    assert result.returncode == 1 and manifest["failures"] == ["temporal"]
    assert manifest["components"]["temporal"] == [] and not list(dest.glob("temporal*.dump"))


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


def test_the_legacy_temporal_sqlite_file_is_not_copied(host):
    """data/temporal.db is dead since Temporal moved to Postgres: it stays on disk and is no longer copied."""
    _store(host.tmp / "data" / "temporal.db", range(3))
    result, dest, manifest, _ = host.run()
    assert result.returncode == 0 and manifest["failures"] == [], result.stdout + result.stderr
    assert not list(dest.glob("temporal.db*")) and (host.tmp / "data" / "temporal.db").is_file()


def test_the_workspace_notes_are_archived_without_git_venv_chat_logs_or_logs(host):
    workspace = host.tmp / "openclaw" / "workspace"
    for name in (".git/config", "chat_logs/x", "moltmarket_venv/y", "a.log", "notes/b.log", "notes/plan.md"):
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (workspace / name).write_text(name)
    (host.tmp / "openclaw" / "cron").mkdir()
    (host.tmp / "openclaw" / "cron" / "jobs.json").write_text("{}")
    result, dest, manifest, _ = host.run()
    assert result.returncode == 0 and manifest["failures"] == [], result.stdout + result.stderr
    with tarfile.open(dest / "workspace.tar.gz") as tar:
        names = set(tar.getnames())
    assert {"workspace", "workspace/AGENTS.md", "workspace/notes/plan.md"} <= names
    assert not names & {"workspace/.git", "workspace/.git/config", "workspace/chat_logs", "workspace/chat_logs/x",
                        "workspace/moltmarket_venv", "workspace/moltmarket_venv/y", "workspace/a.log",
                        "workspace/notes/b.log"}
    assert oct((dest / "workspace.tar.gz").stat().st_mode & 0o777) == "0o600"
    # OpenClaw keeps its cron jobs in its state store, which openclaw-state/ holds.
    assert not (dest / "cron_jobs.json").exists()
    shutil.rmtree(workspace)
    shutil.rmtree(dest)  # a second run within the same second would reuse its directory
    result, dest, manifest, _ = host.run()
    assert result.returncode == 1 and manifest["failures"] == ["workspace"]
    assert f"FAIL: workspace: {workspace} is missing" in result.stdout and not (dest / "workspace.tar.gz").exists()


def _artifact_store(host, **store):
    """The live settings' artifact_store set to ``store``."""
    host.settings.write_text(json.dumps({**json.loads(host.settings.read_text()), "artifact_store": store}))


def test_a_missing_artifact_root_fails_the_artifacts_component(host):
    """The root comes from the settings, and the backup never creates it: a store that moved is not skipped."""
    root = host.tmp / "moved" / "store"
    _artifact_store(host, root_path=str(root))
    result, dest, manifest, _ = host.run()
    assert result.returncode == 1 and manifest["failures"] == ["artifacts"]
    assert f"FAIL: artifacts: the artifact root {root} is missing" in result.stdout
    assert not root.exists() and not (dest / "artifacts.tar.gz").exists()
    # A disabled store has nothing to archive, and that is no failure.
    _artifact_store(host, enabled=False, root_path=str(root))
    shutil.rmtree(dest)  # a second run within the same second would reuse its directory
    result, dest, manifest, _ = host.run()
    assert result.returncode == 0 and manifest["failures"] == [], result.stdout + result.stderr
    assert not root.exists() and not (dest / "artifacts.tar.gz").exists()


def test_a_moved_artifact_store_is_backed_up_and_restored(host):
    """Settings name a root outside data/, under another name: the backup archives it, and the restore puts it back
    there, from a backup made since the move and from one made before it."""
    # The backup before the move, under an older stamp: the next run may start within the same second.
    before_the_move = host.run()[1].rename(host.backups / "20261001T031500Z")
    root = host.tmp / "elsewhere" / "evidence"
    root.mkdir(parents=True)
    (root / "report.json").write_text('{"v": 1}')
    _artifact_store(host, root_path=str(root))
    result, dest, manifest, _ = host.run()
    assert result.returncode == 0 and manifest["failures"] == [], result.stdout + result.stderr
    assert tarfile.open(dest / "artifacts.tar.gz").getnames() == ["evidence", "evidence/report.json"]
    (root / "report.json").write_text('{"v": 2}')
    (root / "added.json").write_text("{}")
    result = host.restore(dest)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in root.iterdir()) == ["report.json"]
    assert (root / "report.json").read_text() == '{"v": 1}'
    # Extracted beside the root, then moved into its place: nothing is left beside it.
    assert [p.name for p in root.parent.iterdir()] == ["evidence"]
    assert (host.tmp / "data" / "artifacts" / "report.json").read_text() == "{}"
    # An archive made before the move holds the old root's name; it goes to the configured root all the same.
    _artifact_store(host, root_path=str(root))
    result = host.restore(before_the_move)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in root.iterdir()) == ["report.json"] and (root / "report.json").read_text() == "{}"
    assert [p.name for p in root.parent.iterdir()] == ["evidence"]


def test_a_symlinked_artifact_root_is_archived_as_its_directory_and_restored_through_its_link(host):
    """tar stores a symlink operand as the link alone: the backup archives the directory the root resolves to, and the
    restore replaces that directory and keeps the link."""
    store = host.tmp / "disk" / "store"
    store.mkdir(parents=True)
    (store / "report.json").write_text('{"v": 1}')
    root = host.tmp / "elsewhere" / "evidence"
    root.parent.mkdir()
    root.symlink_to(store)
    _artifact_store(host, root_path=str(root))
    result, dest, manifest, _ = host.run()
    assert result.returncode == 0 and manifest["failures"] == [], result.stdout + result.stderr
    assert tarfile.open(dest / "artifacts.tar.gz").getnames() == ["store", "store/report.json"]
    (store / "report.json").write_text('{"v": 2}')
    (store / "added.json").write_text("{}")
    result = host.restore(dest)
    assert result.returncode == 0, result.stdout + result.stderr
    assert root.is_symlink() and os.readlink(root) == str(store)
    assert sorted(p.name for p in store.iterdir()) == ["report.json"]
    assert (store / "report.json").read_text() == '{"v": 1}'
    assert [p.name for p in store.parent.iterdir()] == ["store"]


@pytest.mark.parametrize("entries, count", [(("artifacts/report.json", "stray.json"), 2), (("artifacts",), 1)])
def test_an_artifacts_archive_without_exactly_one_top_level_directory_is_not_restored(host, entries, count):
    """The archive's one top-level directory replaces the root; with any other shape the root stays as it is, and
    nothing is left beside it. A lone top-level entry must be a directory itself, not a link to one."""
    _, dest, _, _ = host.run()
    root = host.tmp / "data" / "artifacts"
    elsewhere = host.tmp / "elsewhere"
    elsewhere.mkdir()
    with tarfile.open(dest / "artifacts.tar.gz", "w:gz") as tar:
        for name in entries:
            info = tarfile.TarInfo(name)
            if name == "artifacts":
                info.type, info.linkname = tarfile.SYMTYPE, str(elsewhere)
            tar.addfile(info, io.BytesIO(b""))
    _artifact_store(host, root_path=str(root))
    tree = {str(p): _sha256(p) if p.is_file() else None for p in root.parent.rglob("*")}
    result = host.restore(dest)
    assert result.returncode == 1
    assert f"artifacts: artifacts.tar.gz holds {count} top-level entries, not one directory\n" in result.stderr
    assert "Restore INCOMPLETE: not restored: artifacts\n" in result.stderr
    assert {str(p): _sha256(p) if p.is_file() else None for p in root.parent.rglob("*")} == tree


def test_a_failed_move_keeps_the_restored_artifacts_beside_the_root(host):
    """Once the old root is removed, the restored copy is the only one on disk: a failed mv keeps it beside the root,
    and the restore says where, instead of deleting it with its temporary directory."""
    _, dest, _, _ = host.run()
    root = host.tmp / "data" / "artifacts"
    _artifact_store(host, root_path=str(root))
    mv = host.tmp / "bin" / "mv"
    mv.write_text("#!/bin/sh\necho 'mv: cannot move: Device or resource busy' >&2\nexit 1\n")
    mv.chmod(0o755)
    result = host.restore(dest)
    assert result.returncode == 1
    assert "Restore INCOMPLETE: not restored: artifacts\n" in result.stderr
    [kept] = root.parent.glob(".artifacts.restoring.*")
    assert (f"artifacts: could not move the restored copy into {root}; it is kept in {kept}\n") in result.stderr
    assert (kept / "artifacts" / "report.json").read_text() == "{}" and not root.exists()


@pytest.mark.parametrize("root", ["ancestor", "relative"])
def test_the_restore_refuses_an_artifact_root_that_would_delete_more_than_artifacts(host, root):
    """A root that holds the backups (here the data directory), or a relative one, which would resolve against
    wherever the restore runs, is refused, and nothing under it is touched. / is not run as a case: a broken guard
    would delete the host. The ancestor rule refuses it too, since / holds RMP_ROOT."""
    _, dest, _, _ = host.run()
    data = host.tmp / "data"
    root, reason = ((str(data), f"it is or holds {os.path.realpath(data)}") if root == "ancestor"
                    else ("data/artifacts", "it is not an absolute path"))
    _artifact_store(host, root_path=root)
    # Made since the backup: a restore of the root, refused or not, would remove it.
    (data / "artifacts" / "live.json").write_text("{}")
    tree = {str(p): _sha256(p) if p.is_file() else None for p in data.rglob("*")}
    result = host.restore(dest)   # from tmp_path, where data/artifacts is the artifact store
    assert result.returncode == 1
    refusal = f"artifacts: refusing to replace {root}: {reason}; restore artifacts.tar.gz by hand\n"
    assert refusal in result.stderr
    assert "Restore INCOMPLETE: not restored: artifacts\n" in result.stderr.split(refusal, 1)[1]
    assert {str(p): _sha256(p) if p.is_file() else None for p in data.rglob("*")} == tree


@pytest.mark.parametrize("holds", ["the backup", "the backup root", "the data directory"])
def test_the_restore_refuses_an_artifact_root_that_holds_a_guarded_path_without_being_one(host, holds):
    """The ancestor rule on its own: each root here equals none of the checkout, the data directory, the backup root
    and the backup restored, and holds one of them. None is tmp_path itself, whose removal by a broken guard would
    take the fake tools with it."""
    _, dest, _, _ = host.run()
    src, extra = dest, {}
    if holds == "the backup":
        root = host.tmp / "rescue"
        src = guarded = root / dest.name
        shutil.copytree(dest, src)
    elif holds == "the backup root":
        root = host.tmp / "vault"
        guarded = root / "backups"
        shutil.copytree(dest, guarded / dest.name)
        extra["RMP_BACKUP_ROOT"] = str(guarded)
    else:
        root = host.tmp / "deep"
        guarded = root / "data"
        shutil.copytree(host.tmp / "data" / "artifacts", guarded / "artifacts")
        extra["RMP_DATA_DIR"] = str(guarded)
    _artifact_store(host, root_path=str(root))
    watched = (host.tmp / "data", root)
    tree = {str(p): _sha256(p) if p.is_file() else None for d in watched for p in d.rglob("*")}
    result = host.restore(src, **extra)
    assert result.returncode == 1
    refusal = (f"artifacts: refusing to replace {root}: it is or holds {os.path.realpath(guarded)}; "
               "restore artifacts.tar.gz by hand\n")
    assert refusal in result.stderr
    assert "Restore INCOMPLETE: not restored: artifacts\n" in result.stderr.split(refusal, 1)[1]
    assert {str(p): _sha256(p) if p.is_file() else None for d in watched for p in d.rglob("*")} == tree


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


def _python_without_the_manifest_module(host):
    """An RMP_PYTHON that runs everything but ``-m app.production.backup_manifest``, which fails as a crash would."""
    python = host.tmp / "python"
    python.write_text('#!/bin/sh\nif [ "$1 $2" = "-m app.production.backup_manifest" ]; then\n'
                      '  echo "Traceback: the manifest module crashed" >&2\n  exit 1\nfi\n'
                      f'exec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    return str(python)


def test_a_run_whose_manifest_is_not_written_fails_and_prunes_nothing(host):
    """Without a manifest nothing records what the run holds: it is a failed run, whatever its components did."""
    _dated(host.backups, range(1, 17), time.time() - 30 * 86400)
    result, dest, manifest, _ = host.run(RMP_PYTHON=_python_without_the_manifest_module(host))
    assert result.returncode == 1 and manifest is None and not list(dest.glob("manifest.json*"))
    assert "FAIL: manifest: the manifest was not written" in result.stdout
    assert "Backup INCOMPLETE: manifest failed" in result.stdout and "no older backup was pruned" in result.stdout
    assert "the manifest module crashed" in result.stderr
    assert len([p for p in host.backups.iterdir() if p.name[:2] == "20"]) == 17


def test_a_second_run_while_one_holds_the_lock_exits_without_touching_anything(host):
    import fcntl
    host.backups.mkdir(parents=True)
    with open(host.backups / ".backup.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result, dest, _, calls = host.run()
    assert result.returncode == 1 and "another backup is running" in result.stderr
    assert dest is None and calls == []


def _offsite(host, enabled):
    """The live settings' backup.offsite.enabled set to ``enabled``."""
    host.settings.write_text(json.dumps({**json.loads(host.settings.read_text()),
                                         "backup": {"offsite": {"enabled": enabled}}}))


def _restic_env(host, mode=0o600, **variables):
    """The off-host copy's env file, as /etc/rmp/restic.env holds it, with ``mode``."""
    path = host.tmp / "restic.env"
    path.write_text("".join(f"{name}={value}\n" for name, value in variables.items()))
    path.chmod(mode)
    return path


def _s3_variables(host):
    """What an env file for an S3 bucket holds; the values are made up."""
    return {"RESTIC_REPOSITORY": "s3:https://s3.example.net/rmp-backups",
            "RESTIC_PASSWORD_FILE": str(host.tmp / "restic-password"),
            "AWS_ACCESS_KEY_ID": "made-up-access-key-id", "AWS_SECRET_ACCESS_KEY": "made-up-secret-access-key"}


def test_the_offsite_copy_is_off_by_default_and_the_manifest_says_so(host):
    """Accepted exception K-7: unless backup.offsite.enabled is true, restic never runs and its env file is never
    read."""
    for setting in ("unset", "false"):
        if setting == "false":
            _offsite(host, False)
        result, dest, manifest, _ = host.run(RMP_RESTIC_ENV=str(host.tmp / "no-such-restic.env"))
        assert result.returncode == 0 and manifest["failures"] == [], result.stdout + result.stderr
        assert manifest["offsite"] == "off" and "Off-host copy: off" in result.stdout
        assert not (host.state / "restic.log").exists()
        shutil.rmtree(dest)  # a second run within the same second would reuse its directory


def _python_counting_restic_variables(host):
    """An RMP_PYTHON that records, for each run, how many RESTIC_ and AWS_ variables its environment holds."""
    python = host.tmp / "python"
    python.write_text('#!/bin/sh\nenv | grep -c -E "^(RESTIC|AWS)_" >> "${FAKE_STATE}/python-env.log"\n'
                      f'exec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    return str(python)


def test_the_offsite_copy_runs_restic_with_the_env_file(host):
    _offsite(host, True)
    variables = _s3_variables(host)
    env_file = _restic_env(host, **variables)
    inherited = {name: None for name in os.environ if name.startswith(("RESTIC_", "AWS_"))}
    result, dest, manifest, _ = host.run(RMP_RESTIC_ENV=str(env_file),
                                         RMP_PYTHON=_python_counting_restic_variables(host), **inherited)
    assert result.returncode == 0 and manifest["failures"] == [], result.stdout + result.stderr
    assert manifest["offsite"] == "copied"
    calls = host.log("restic.log")
    hostname = socket.gethostname()
    assert [call["argv"] for call in calls] == [
        ["backup", "--quiet", "--tag", "rmp-backup", "--host", hostname, str(dest)],
        ["forget", "--quiet", "--tag", "rmp-backup", "--host", hostname, "--group-by", "host", "--keep-last", "14",
         "--prune"]]
    # The file's variables reach restic, and its values reach no command line, no output and no log.
    for call in calls:
        assert {name: call["env"].get(name) for name in variables} == variables
    shown = result.stdout + result.stderr + (dest / "backup.log").read_text()
    for value in variables.values():
        assert value not in shown and not any(value in word for call in calls for word in call["argv"])
    # Read in a subshell, they reach no other step: not the manifest's record of the copy, nor the pruning after it.
    counts = (host.state / "python-env.log").read_text().split()
    assert len(counts) >= 5 and set(counts) == {"0"}
    # The copy runs after a local failure too.
    shutil.rmtree(dest)  # a second run within the same second would reuse its directory
    result, dest, manifest, _ = host.run(RMP_RESTIC_ENV=str(env_file), FAKE_QDRANT_DOWN="1")
    assert result.returncode == 1 and manifest["failures"] == ["qdrant"] and manifest["offsite"] == "copied"
    assert [call["argv"][0] for call in host.log("restic.log")] == ["backup", "forget"]


def test_a_failed_offsite_copy_fails_the_run(host):
    _dated(host.backups, range(1, 17), time.time() - 30 * 86400)
    _offsite(host, True)
    result, dest, manifest, _ = host.run(RMP_RESTIC_ENV=str(_restic_env(host, **_s3_variables(host))),
                                         FAKE_RESTIC_FAIL="backup")
    assert result.returncode == 1
    assert manifest["failures"] == ["offsite"] and manifest["offsite"] == "failed"
    assert [call["argv"][0] for call in host.log("restic.log")] == ["backup"]
    assert "FAIL: offsite: restic did not copy the backup (see backup.log)" in result.stdout
    assert "Backup INCOMPLETE: offsite failed" in result.stdout and "no older backup was pruned" in result.stdout
    # restic's own message goes to the log, not to the journal.
    assert "Fatal: unable to open config file" in (dest / "backup.log").read_text()
    assert "Fatal" not in result.stdout + result.stderr
    assert len([p for p in host.backups.iterdir() if p.name[:2] == "20"]) == 17


@pytest.mark.parametrize("case", ["readable by its group", "owned by another user", "missing"])
def test_an_env_file_readable_by_others_is_refused(host, case):
    """The env file holds the repository's secrets: one that is missing, that another user may read, or that belongs
    to someone else is never read, and the copy fails."""
    _offsite(host, True)
    mode = 0o640 if case == "readable by its group" else 0o600
    env_file = _restic_env(host, mode=mode, **_s3_variables(host))
    runs_as = os.getuid()
    if case == "owned by another user":
        # The run believes it runs as another user: giving the file away would need root.
        runs_as += 1
        fake_id = host.tmp / "bin" / "id"
        fake_id.write_text(f'#!/bin/sh\n[ "$*" = -u ] && echo {runs_as} || exec /usr/bin/id "$@"\n')
        fake_id.chmod(0o755)
    elif case == "missing":
        env_file.unlink()
    result, dest, manifest, _ = host.run(RMP_RESTIC_ENV=str(env_file))
    assert result.returncode == 1
    assert manifest["failures"] == ["offsite"] and manifest["offsite"] == "failed"
    assert not (host.state / "restic.log").exists()
    reason = (f"{env_file} is missing" if case == "missing" else
              f"refusing {env_file}: its owner and mode are '{os.getuid()} {mode:o}', not '{runs_as} 600'")
    assert f"FAIL: offsite: {reason}" in result.stdout


@pytest.mark.parametrize("backup", [{"offsite": True}, "on"], ids=["offsite not an object", "backup not an object"])
def test_an_offsite_setting_that_cannot_be_read_fails_the_copy(host, backup):
    """A malformed setting may be one that asks for the copy: it fails the run instead of passing for a copy switched
    off. The env file is never read, and Qdrant and the artifacts, read from the same settings first, are backed up."""
    host.settings.write_text(json.dumps({**json.loads(host.settings.read_text()), "backup": backup}))
    result, dest, manifest, _ = host.run(RMP_RESTIC_ENV=str(host.tmp / "no-such-restic.env"))
    assert result.returncode == 1
    assert manifest["failures"] == ["offsite"] and manifest["offsite"] == "failed"
    assert not (host.state / "restic.log").exists()
    assert f"FAIL: offsite: could not read backup.offsite from {host.settings} (see backup.log)" in result.stdout
    assert "AttributeError" in (dest / "backup.log").read_text()
    assert manifest["components"]["qdrant"] == ["qdrant-full.snapshot"]
    assert manifest["components"]["artifacts"] == ["artifacts.tar.gz"]


def _wait_for_a_new_stamp(dest):
    """The stamp is per second: wait until the next backup gets a directory of its own."""
    while time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) <= dest.name:
        time.sleep(0.1)


@pytest.mark.skipif(RESTIC is None, reason="restic is not installed")
def test_a_restic_round_trip_against_a_local_repository(host):
    """The real restic, against a repository in tmp_path: two nights' backups copied, the newest restored whole. The
    fake directory's restic is the host's binary here, the one tool of a backup run that is not a fake."""
    fake_restic = host.tmp / "bin" / "restic"
    fake_restic.unlink()
    fake_restic.symlink_to(RESTIC)
    work = host.tmp / "restic"
    work.mkdir(mode=0o700)
    password = work / "password"
    password.write_text(secrets.token_hex(32))
    password.chmod(0o600)
    variables = {"RESTIC_REPOSITORY": str(work / "repo"), "RESTIC_PASSWORD_FILE": str(password),
                 "RESTIC_CACHE_DIR": str(work / "cache")}
    env_file = _restic_env(host, **variables)
    restic_env = {**{name: value for name, value in os.environ.items() if not name.startswith("RESTIC_")}, **variables}

    def restic(*args):
        return subprocess.run([RESTIC, *args], env=restic_env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=300, check=True).stdout

    restic("init")
    _offsite(host, True)
    copied = []
    for _ in range(2):
        if copied:
            _wait_for_a_new_stamp(copied[-1])
        result, dest, manifest, _ = host.run(RMP_RESTIC_ENV=str(env_file))
        assert result.returncode == 0 and manifest["failures"] == [], result.stdout + result.stderr
        assert manifest["offsite"] == "copied"
        copied.append(dest)
    snapshots = json.loads(restic("snapshots", "--json"))
    assert sorted((s["paths"], s["tags"], s["hostname"]) for s in snapshots) == [
        ([str(dest)], ["rmp-backup"], socket.gethostname()) for dest in copied]
    out = host.tmp / "out"
    restic("restore", "latest", "--target", str(out))
    restored = out / copied[-1].relative_to("/")
    assert manifest["files"] and all(_sha256(restored / name) == meta["sha256"]
                                     for name, meta in manifest["files"].items())
    # The copy is made once the manifest is written: the restored copy verifies on its own, as the restore checks it
    # first, and holds the manifest as written, before the run recorded the copy in it.
    verify = subprocess.run([sys.executable, "-m", "app.production.backup_manifest", "verify", str(restored)], cwd=REPO,
                            capture_output=True, text=True, timeout=120)
    assert verify.returncode == 0 and verify.stdout == "" and verify.stderr == "", verify.stdout + verify.stderr
    assert json.loads((restored / "manifest.json").read_text()) == {
        name: value for name, value in manifest.items() if name != "offsite"}
    assert not any(password.read_text() in text for text in (result.stdout, result.stderr,
                                                             (copied[-1] / "backup.log").read_text()))
    # Each night's directory is a path of its own: grouped by host, the forget still keeps only the newest copies.
    _wait_for_a_new_stamp(copied[-1])
    result, dest, manifest, _ = host.run(RMP_RESTIC_ENV=str(env_file), RMP_OFFSITE_KEEP="1")
    assert result.returncode == 0 and manifest["offsite"] == "copied", result.stdout + result.stderr
    assert [s["paths"] for s in json.loads(restic("snapshots", "--json"))] == [[str(dest)]]


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


def test_the_snapshot_restore_sends_a_given_key_on_stdin_only(host):
    """The restore drill keys its scratch Qdrant: the key reaches curl on stdin, never on a command line any local user
    can read, and the output does not show it."""
    _, dest, _, _ = host.run()
    snapshot = str(dest / "qdrant-full.snapshot")
    result, _, _, _ = host.run(RESTORE_QDRANT, snapshot, "http://127.0.0.1:6937", QDRANT_API_KEY=QDRANT_KEY)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = host.log("curl-argv.log")
    assert [call["argv"][-1].split("/")[-1].split("?")[0] for call in calls] == ["readyz", "upload"]
    assert all(call["argv"][:2] == ["-K", "-"] and call["headers"] == [f"api-key: {QDRANT_KEY}"] for call in calls)
    assert not any(QDRANT_KEY in word for call in calls for word in call["argv"])
    assert QDRANT_KEY not in result.stdout + result.stderr
    # Empty, there is no key. The suite never runs a script without the variable, which would read /etc/rmp/rmp.env.
    result, _, _, _ = host.run(RESTORE_QDRANT, snapshot, "http://127.0.0.1:6937", QDRANT_API_KEY="")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = host.log("curl-argv.log")
    assert len(calls) == 2 and not any("-K" in call["argv"] or call["headers"] for call in calls)


def test_qdrant_curl_sends_the_key_on_stdin_and_fails_when_it_cannot_read_it(host):
    """ops/qdrant_curl.sh, every script's Qdrant call and the operator's, resolves the key as qdrant_api_key() does.
    QDRANT_API_KEY, when it is set, even to "", is the key, stripped, and no interpreter starts. Without it, RMP_PYTHON
    runs qdrant_api_key() in RMP_ROOT, which reads /etc/rmp/rmp.env; a fake interpreter stands in for it here. curl gets
    the key as a config line on stdin and every other argument as given, and a key it cannot read is a failed call."""
    url = "http://127.0.0.1:6333/readyz"
    fake, ran = host.tmp / "bin", host.state / "python.log"
    python = fake / "python-reading-the-key-file"
    python.write_text('#!/bin/sh\nprintf \'%s|%s\\n\' "$PWD" "$*" >> "${FAKE_STATE}/python.log"\n'
                      'echo key-from-the-file\n')
    broken = fake / "python-that-fails"
    broken.write_text("#!/bin/sh\nexit 1\n")
    for interpreter in (python, broken):
        interpreter.chmod(0o755)

    result, _, _, _ = host.run(QDRANT_CURL, "-fsS", url, QDRANT_API_KEY=f" {QDRANT_KEY}\n", RMP_PYTHON=str(python))
    assert result.returncode == 0, result.stdout + result.stderr
    assert host.log("curl-argv.log") == [{"argv": ["-K", "-", "-fsS", url], "headers": [f"api-key: {QDRANT_KEY}"]}]
    assert QDRANT_KEY not in result.stdout + result.stderr
    result, _, _, _ = host.run(QDRANT_CURL, "-fsS", url, QDRANT_API_KEY="", RMP_PYTHON=str(python))
    assert result.returncode == 0, result.stdout + result.stderr
    assert host.log("curl-argv.log") == [{"argv": ["-fsS", url], "headers": []}]
    assert not ran.exists()

    result, _, _, _ = host.run(QDRANT_CURL, "-fsS", url, QDRANT_API_KEY=None, RMP_PYTHON=str(python))
    assert result.returncode == 0, result.stdout + result.stderr
    assert ran.read_text().splitlines() == [
        f"{REPO}|-c from app.memory.vector import qdrant_api_key; print(qdrant_api_key())"]
    assert host.log("curl-argv.log") == [{"argv": ["-K", "-", "-fsS", url], "headers": ["api-key: key-from-the-file"]}]
    result, _, _, _ = host.run(QDRANT_CURL, "-fsS", url, QDRANT_API_KEY=None, RMP_PYTHON=str(broken))
    assert result.returncode == 1 and "qdrant_curl: could not read Qdrant's API key" in result.stderr
    assert host.log("curl-argv.log") == []


def test_restore_warns_before_its_prompt_when_a_file_does_not_match_its_checksum(host):
    """A disaster may leave only a damaged backup: the restore verifies it, warns before its prompt, and the operator
    decides. The backup's path may be relative to where the restore runs."""
    _, dest, _, _ = host.run()
    dump = dest / "rmp_db.dump"
    dump.write_bytes(dump.read_bytes()[:-1] + b"!")
    for src in (dest, dest.relative_to(host.tmp)):
        result = host.restore(src, answer="n\n")
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.startswith("WARNING: this backup does not verify:\n"
                                        "rmp_db.dump does not match its checksum\n"
                                        f"=== RMP Restore from {src} ===\n"), result.stdout
        assert host.log("systemctl.log") == [] and host.log("sudo.log") == []
    # A backup made before manifest format 2 gets its note; one that verifies, no warning.
    dump.write_bytes(b"PGDMP")
    manifest = dest / "manifest.json"
    complete = manifest.read_text()
    manifest.write_text(json.dumps({"timestamp": dest.name, "failures": []}))
    result = host.restore(dest, answer="n\n")
    assert result.stdout.startswith("WARNING: this backup does not verify:\n"
                                    "made before manifest format 2 (no file list)\n"
                                    "=== RMP Restore from "), result.stdout
    manifest.write_text(complete)
    result = host.restore(dest, answer="n\n")
    assert result.returncode == 0 and result.stdout.startswith("=== RMP Restore from "), result.stdout
    assert "WARNING" not in result.stdout + result.stderr


def test_restore_warns_before_its_prompt_when_the_check_itself_cannot_run(host):
    """A fresh host whose venv is not built cannot run the check: the warning says so, and its error is on stderr."""
    _, dest, _, _ = host.run()
    result = host.restore(dest, answer="n\n", RMP_PYTHON=_python_without_the_manifest_module(host))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("WARNING: this backup does not verify:\n"
                                    "the check itself could not run (its error is on stderr)\n"
                                    "=== RMP Restore from "), result.stdout
    assert "the manifest module crashed" in result.stderr
    assert host.log("systemctl.log") == [] and host.log("sudo.log") == []


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
    starts = [(i, argv[1], record["settings_sha256"])
              for i, (_, argv, record) in enumerate(calls) if argv[0] == "start"]
    assert [unit for _, unit, _ in starts] == STARTS
    assert all(i > databases[-1] for i, _, _ in starts)
    # Every start saw the backup's settings in place: the starts follow the settings copy.
    assert {sha for _, _, sha in starts} == {_sha256(dest / "settings.json")}
    assert calls[-1][1][0] == "list-timers"
    assert result.stdout.count("\nnext: ") == len(TIMERS) and "NO NEXT" not in result.stdout
    assert result.stdout.rstrip().endswith("Restore complete.") and "Restore ended" not in result.stderr


def test_a_gateway_waiting_to_restart_is_stopped_and_started_again_and_an_inactive_code_watch_is_left_alone(host):
    """The gateway has Restart=always: between two runs it is activating, and its next run would write OpenClaw's
    stores mid-restore. The OpenClaw tool's own check misses that: it refuses only an active gateway."""
    _, dest, _, _ = host.run()
    (host.state / "units" / "openclaw-gateway.service").write_text("activating")
    (host.state / "units" / "rmp-code-watch.service").write_text("inactive")
    result = host.restore(dest)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _in_order(host)
    first = next(i for i, (tool, _, _) in enumerate(calls) if tool == "sudo")
    stops = [unit for unit in STOPS if unit != "rmp-code-watch"]
    assert [argv[1] for _, argv, _ in calls[:first] if argv[0] == "stop"] == _verbs(host, "stop") == stops
    assert _verbs(host, "start") == [unit for unit in STARTS if unit != "rmp-code-watch"]


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


@pytest.mark.parametrize("signal, status", [("HUP", "129"), ("INT", "130"), ("TERM", "143"), ("USR1", "1")])
def test_a_restore_ended_by_a_signal_after_its_first_database_step_starts_nothing(host, signal, status):
    """A hung-up SSH session sends HUP, Ctrl-C INT, a kill TERM, each reported with its own status. Bash runs the EXIT
    trap with status 0 after a signal the script does not trap, such as USR1, and the killed step never reports its
    failure: either way the stores may be half restored."""
    _, dest, _, _ = host.run()
    result = host.restore(dest, FAKE_PGRESTORE_SIGNAL=f"temporal={signal}")
    assert result.returncode != 0
    assert len(host.log("sudo.log")) == 2, "the restore went on after the signal"
    assert _verbs(host, "stop") == STOPS and _verbs(host, "start") == []
    units = " ".join(STARTS)
    assert f"Restore INCOMPLETE: it ended before its last step (exit {status})\n" in result.stderr
    assert "not restored:" not in result.stderr
    assert f"Left stopped, because a store may be half restored: {units}\n" in result.stderr
    assert f"When the stores are whole, start them with: systemctl start {units}\n" in result.stderr
    assert "Restore complete." not in result.stdout


@pytest.mark.parametrize("signal, status", [("HUP", "129"), ("USR1", "1")])
def test_a_restore_ended_by_a_signal_before_its_first_database_step_starts_what_it_stopped(host, signal, status):
    """Including the unit whose stop the signal interrupted, and without claiming that the restore completed."""
    _, dest, _, _ = host.run()
    result = host.restore(dest, FAKE_SIGNAL_ON_STOP=f"temporal={signal}")
    assert result.returncode != 0
    assert host.log("sudo.log") == []
    assert _verbs(host, "stop") == STOPS and _verbs(host, "start") == STARTS
    assert f"Restore ended before its first database step (exit {status}): nothing was restored\n" in result.stderr
    assert "Restore complete." not in result.stdout and "INCOMPLETE" not in result.stderr


@pytest.mark.parametrize("output", ["pipe", "terminal"])
def test_a_restore_whose_session_hangs_up_before_its_first_database_step_starts_what_it_stopped(host, output):
    """A hang-up takes the output with it: a write to the hung-up terminal fails with EIO, one to the pipe of a
    `| tee` that died raises SIGPIPE. The login shell sends the restore HUP, and the kernel sends it once more when that
    shell exits, which can be while the restore starts the units. The hang-up also kills the `systemctl stop` running
    at that moment, and bash then reports that child on the dead output. The restore writes nothing between its prompt
    and the stops."""
    _, dest, _, _ = host.run()
    hang_up = {"FAKE_SIGNAL_ON_STOP": "temporal=HUP+self", "FAKE_SIGNAL_ON_START": "temporal=HUP"}
    if output == "pipe":
        restore = host.spawn(dest, subprocess.PIPE, **hang_up)
        reader = restore.stdout
    else:
        terminal, line = os.openpty()
        restore = host.spawn(dest, line, **hang_up)
        os.close(line)
        reader = open(terminal, "rb")
    assert reader.readline().startswith(b"=== RMP Restore from ")
    reader.close()
    restore.stdin.write(b"y\n")
    restore.stdin.close()
    assert restore.wait(timeout=300) == 129
    assert host.log("sudo.log") == []
    assert _verbs(host, "stop") == STOPS and _verbs(host, "start") == STARTS
    # The second HUP above lands inside finish, which ignores HUP itself. One between the HUP trap and finish's first
    # line would end the restore before it starts anything; no fake can time a signal into that window, so the trap
    # that ignores it is pinned by its text.
    assert "\ntrap 'trap \"\" HUP; exit 129' HUP\n" in RESTORE.read_text()


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
    assert "Restore ended before its first database step (exit 1): nothing was restored\n" in result.stderr
    assert host.log("sudo.log") == []
    assert _verbs(host, "start") == [unit for unit in STARTS if unit not in ("rmp-worker", "temporal")]


@pytest.mark.parametrize("run_state", ["active", "activating"])
def test_a_timer_whose_run_is_in_progress_after_the_restart_is_waited_for(host, run_state):
    """A started timer whose interval passed during the restore runs its service at once and shows no next elapse
    until that run ends. Every RMP timer's service is Type=oneshot, which shows activating while it runs."""
    _, dest, _, _ = host.run()
    result = host.restore(dest, FAKE_TIMER_RUN="rmp-temporal-watchdog.timer=3", FAKE_TIMER_RUN_STATE=run_state)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "NO NEXT:" not in result.stdout + result.stderr
    assert (host.state / "sleep.log").read_text().splitlines() == ["10"] * 3
    result = host.restore(dest, FAKE_TIMER_RUN="rmp-temporal-watchdog.timer=100", FAKE_TIMER_RUN_STATE=run_state)
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
