"""ops/start_qdrant.sh and ops/stop_qdrant.sh, rmp-qdrant.service's start and stop, against a fake docker and curl.

The fake docker records each call's argv and keeps whether the container rmp-qdrant exists in a state file; the fake
curl answers Qdrant's readyz while it does. The storage is under RMP_DATA_DIR in tmp_path. A script runs only when
every tool it could reach on the host resolves to its fake and the start takes its storage from RMP_DATA_DIR: the
production guard does not see a child process's own calls.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
START = REPO / "ops" / "start_qdrant.sh"
STOP = REPO / "ops" / "stop_qdrant.sh"
INSPECT = ["container", "inspect", "rmp-qdrant"]
ONE_OFF = "rmp-qdrant-from-snapshot"
INSPECT_ONE_OFF = ["container", "inspect", ONE_OFF]
TIMERS = sorted(u for u in (REPO / "ops" / "systemd" / "enabled-units.txt").read_text().splitlines()
                if u.endswith(".timer"))
# docker run's message when the port is taken, one way a run fails.
RUN_FAILED = ("docker: Error response from daemon: driver failed programming external connectivity on endpoint "
              "rmp-qdrant: Bind for 127.0.0.1:6333 failed: port is already allocated.")

FAKE_DOCKER = r'''#!/usr/bin/env python3
"""docker, as far as the Qdrant scripts use it. Every call's argv goes to ${FAKE_STATE}/docker.log; rmp-qdrant exists
while ${FAKE_STATE}/container does, and another container <name> while ${FAKE_STATE}/container-<name> does.
FAKE_DOCKER_FAIL=<run|stop> fails that command with docker's message."""
import json, os, sys
args, state = sys.argv[1:], os.environ["FAKE_STATE"]
verb = args[1] if args[0] == "container" else args[0]
name = args[-1] if verb in ("inspect", "stop", "rm", "logs") else "rmp-qdrant"
container = os.path.join(state, "container" if name == "rmp-qdrant" else "container-" + name)
with open(os.path.join(state, "docker.log"), "a") as log:
    log.write(json.dumps(args) + "\n")
if os.environ.get("FAKE_DOCKER_FAIL") == verb:
    print(os.environ["FAKE_DOCKER_MESSAGE"], file=sys.stderr)
    sys.exit(125 if verb == "run" else 1)
if verb in ("inspect", "stop", "rm", "logs") and not os.path.exists(container):
    print("Error response from daemon: No such container: " + name, file=sys.stderr)
    sys.exit(1)
if verb == "inspect":
    print(json.dumps([{"Name": "/" + name}]))
elif verb in ("stop", "rm"):
    if verb == "rm":
        os.remove(container)
    print(args[-1])
elif verb == "run":
    if os.path.exists(container):
        print('docker: Error response from daemon: Conflict. The container name "/rmp-qdrant" is already in use.',
              file=sys.stderr)
        sys.exit(125)
    open(container, "w").close()
    print("4f1d0c9e2b7a")
elif verb == "logs":
    print("Qdrant HTTP listening on 6333")
else:
    sys.exit(1)
'''

# The host has /usr/bin/docker-compose, which the scripts used before; a call is recorded with docker's and fails.
FAKE_DOCKER_COMPOSE = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.path.join(os.environ["FAKE_STATE"], "docker.log"), "a") as log:
    log.write(json.dumps(["docker-compose", *sys.argv[1:]]) + "\n")
sys.exit(1)
'''

# curl -sf http://127.0.0.1:6333/readyz: ready while the container exists.
FAKE_CURL = r'''#!/bin/sh
printf '%s\n' "$*" >> "${FAKE_STATE}/curl.log"
case "$*" in
  *http://127.0.0.1:6333/readyz*) [ -e "${FAKE_STATE}/container" ] && exit 0; exit 7 ;;
esac
exit 22
'''

FAKE_SLEEP = r'''#!/bin/sh
printf '%s\n' "$*" >> "${FAKE_STATE}/sleep.log"
'''

FAKES = {"docker": FAKE_DOCKER, "docker-compose": FAKE_DOCKER_COMPOSE, "curl": FAKE_CURL, "sleep": FAKE_SLEEP}


def _runs_only_against_fakes(script, env, fake):
    """The precondition of every script run here: each tool it could reach on the host resolves to its fake, and the
    start takes its storage from RMP_DATA_DIR. A start that hard-codes the live storage is never executed."""
    if script == START:
        assert re.search(r'^DATA_DIR="\$\{RMP_DATA_DIR:-', script.read_text(), re.M), f"{script} hard-codes its data"
    for tool in FAKES:
        assert shutil.which(tool, path=env["PATH"]) == str(fake / tool), f"{tool} does not resolve to its fake"


@pytest.fixture
def qdrant(tmp_path):
    fake, state, data = tmp_path / "bin", tmp_path / "state", tmp_path / "data"
    for d in (fake, state, data):
        d.mkdir()
    for name, body in FAKES.items():
        (fake / name).write_text(body)
        (fake / name).chmod(0o755)
    env = {**os.environ, "RMP_DATA_DIR": str(data), "PATH": f"{fake}:{os.environ['PATH']}", "FAKE_STATE": str(state)}

    def run(script, container, one_off=False, **extra):
        """``script`` with the container present or not, and the one-off restore container too; the fakes' logs start
        empty."""
        run_env = {**env, **extra}
        _runs_only_against_fakes(script, run_env, fake)
        for name in ("docker.log", "curl.log", "sleep.log", "container", f"container-{ONE_OFF}"):
            (state / name).unlink(missing_ok=True)
        if container:
            (state / "container").touch()
        if one_off:
            (state / f"container-{ONE_OFF}").touch()
        return subprocess.run(["bash", str(script)], env=run_env, cwd=tmp_path, capture_output=True, text=True,
                              timeout=60)

    def log(name):
        path = state / name
        return path.read_text().splitlines() if path.exists() else []

    return type("Qdrant", (), {"run": staticmethod(run), "log": staticmethod(log),
                               "docker": staticmethod(lambda: [json.loads(line) for line in log("docker.log")]),
                               "exists": staticmethod(lambda: (state / "container").exists()),
                               "storage": data / "qdrant-server"})


def _run_argv(storage):
    """The docker run of rmp-qdrant: the flags the live container has."""
    return ["run", "-d", "--name", "rmp-qdrant", "--restart", "unless-stopped",
            "-p", "127.0.0.1:6333:6333", "-p", "127.0.0.1:6334:6334",
            "-v", f"{storage}:/qdrant/storage", "-e", "QDRANT__SERVICE__GRPC_PORT=6334", "qdrant/qdrant:v1.17.0"]


def test_start_recreates_the_container_from_its_flags(qdrant):
    run = _run_argv(qdrant.storage)
    result = qdrant.run(START, container=True)
    assert result.returncode == 0, result.stderr
    present = qdrant.docker()
    assert present == [INSPECT_ONE_OFF, INSPECT, ["stop", "rmp-qdrant"], ["rm", "rmp-qdrant"], run]
    # Only docker's stdout is discarded; the readiness wait is what prints.
    assert result.stdout == "Qdrant ready at http://127.0.0.1:6333\n" and qdrant.exists()
    assert qdrant.storage.is_dir()

    result = qdrant.run(START, container=False)
    assert result.returncode == 0, result.stderr
    absent = qdrant.docker()
    assert absent == [INSPECT_ONE_OFF, INSPECT, run]
    # Graceful: stop before rm, and never a forced removal; and no compose anywhere.
    for call in present + absent:
        assert not {"-f", "--force"} & set(call) and not any("compose" in word for word in call)


def test_a_failed_docker_run_fails_the_start(qdrant):
    result = qdrant.run(START, container=False, FAKE_DOCKER_FAIL="run", FAKE_DOCKER_MESSAGE=RUN_FAILED)
    assert result.returncode != 0
    assert RUN_FAILED in result.stderr
    # The start ends at the failure: no readiness wait follows it.
    assert qdrant.docker()[-1][0] == "run" and not qdrant.log("curl.log") and not qdrant.log("sleep.log")


@pytest.mark.parametrize("container", [True, False])
def test_start_is_refused_while_the_one_off_restore_container_rebuilds_the_storage(qdrant, container):
    """Whatever starts rmp-qdrant during the runbook's whole-store restore (the sentinel, a canary's failure path, a
    restart of rmp-api or rmp-worker, which want it) must not put a second Qdrant on the storage the one-off container
    rebuilds: the start inspects the one-off first and touches nothing else."""
    result = qdrant.run(START, container=container, one_off=True)
    assert result.returncode == 1
    assert f"refused: {ONE_OFF} exists and rebuilds {qdrant.storage}" in result.stderr
    assert qdrant.docker() == [INSPECT_ONE_OFF] and qdrant.exists() == container
    assert not qdrant.log("curl.log") and not qdrant.storage.exists()


def test_stop_stops_the_container(qdrant):
    result = qdrant.run(STOP, container=True)
    assert result.returncode == 0, result.stderr
    assert qdrant.docker() == [INSPECT, ["stop", "rmp-qdrant"]] and result.stdout == ""

    result = qdrant.run(STOP, container=False)
    assert result.returncode == 0, result.stderr
    assert qdrant.docker() == [INSPECT]

    message = "Error response from daemon: cannot stop container: rmp-qdrant: permission denied"
    result = qdrant.run(STOP, container=True, FAKE_DOCKER_FAIL="stop", FAKE_DOCKER_MESSAGE=message)
    assert result.returncode != 0 and message in result.stderr
    assert qdrant.docker() == [INSPECT, ["stop", "rmp-qdrant"]]


def test_no_file_names_the_qdrant_compose_file():
    """rmp-qdrant is one docker-run container; the compose file that described another identity of it is gone."""
    name = b"docker-compose.qdrant.yml"
    assert not (REPO / name.decode()).exists()
    files = [p for p in sorted((REPO / "ops").rglob("*")) if p.is_file()]
    files += [REPO / "ARCHITECTURE.md", REPO / "docs" / "VISION_CONFORMANCE.md",
              REPO / "docs" / "runbooks" / "backup-restore.md", REPO / "Makefile"]
    assert [str(p.relative_to(REPO)) for p in files if name in p.read_bytes()] == []


def test_the_whole_store_procedure_keeps_the_sentinel_and_the_backup_away_from_a_stopped_qdrant():
    """The runbook's "Whole store from scratch": the sentinel would start rmp-qdrant, whose start recreates it on the
    storage being rebuilt, and a backup would snapshot that storage, so every timer stops, and its run ends, before
    Qdrant does, and starts only after it runs again; the one-off restore container is never rmp-qdrant."""
    runbook = (REPO / "docs" / "runbooks" / "backup-restore.md").read_text()
    assert "**Whole store from scratch" in runbook
    section = runbook.split("**Whole store from scratch", 1)[1].split("\n## ", 1)[0]

    def at(pattern):
        found = [m.start() for m in re.finditer(pattern, section)]
        assert found, f"the procedure lacks {pattern}"
        return found

    # Every timer stops, not the sentinel's alone: the health and memory canaries run the sentinel when they fail,
    # and the watchdog's recovery restarts rmp-api and rmp-worker, which want rmp-qdrant.
    stop_timers = at(r"systemctl stop " + re.escape(" ".join(TIMERS)) + "\n")[0]
    services = " ".join(timer.removesuffix(".timer") + ".service" for timer in TIMERS)
    asked = at(r"systemctl is-active " + re.escape(services) + "\n")[0]
    assert stop_timers < asked
    stop_qdrant = at(r"systemctl stop rmp-qdrant(?![\w-])")
    assert asked < stop_qdrant[0]
    assert re.findall(r"docker run\b[^\n]*?--name (\S+)", section) == ["rmp-qdrant-from-snapshot"]
    removed = at(r"docker rm rmp-qdrant-from-snapshot")[-1]
    start_qdrant = at(r"systemctl start rmp-qdrant(?![\w-])")
    assert stop_qdrant[-1] < removed < start_qdrant[0]
    start_timers = at(r"systemctl start " + re.escape(" ".join(TIMERS)) + "\n")[0]
    assert start_qdrant[-1] < start_timers
    # The timer check is the procedure's last command.
    check = at(re.escape("""systemctl list-timers 'rmp-*' --all --no-pager --no-legend | awk '{print ($1 == "-" ? """
                         """"NO NEXT: " : "next: ") $(NF-1)}'"""))
    assert check[-1] > start_timers and check[-1] == max(at(r"\b(?:systemctl|docker) "))
