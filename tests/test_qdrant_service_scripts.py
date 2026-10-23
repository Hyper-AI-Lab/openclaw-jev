"""ops/start_qdrant.sh and ops/stop_qdrant.sh, rmp-qdrant.service's start and stop, against a fake docker and curl.

The fake docker records each call's argv and the Qdrant key its environment holds, and keeps whether the container
rmp-qdrant exists in a state file; the fake curl answers Qdrant's readyz while it does, and records the api-key header
it read from stdin. The storage is under RMP_DATA_DIR in tmp_path. A script runs only when every tool it could reach on
the host resolves to its fake, the start takes its storage from RMP_DATA_DIR, and QDRANT_API_KEY is set, so the key is
never read from /etc/rmp/rmp.env: the production guard does not see a child process's own calls.
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
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
# A dummy Qdrant API key: a start with it must pass it to docker by name and to curl on stdin, and never show it.
KEY = "dummy-qdrant-key-" + secrets.token_hex(8)
# docker run's message when the port is taken, one way a run fails.
RUN_FAILED = ("docker: Error response from daemon: driver failed programming external connectivity on endpoint "
              "rmp-qdrant: Bind for 127.0.0.1:6333 failed: port is already allocated.")

FAKE_DOCKER = r'''#!/usr/bin/env python3
"""docker, as far as the Qdrant scripts use it. Every call's argv goes to ${FAKE_STATE}/docker.log, and the value of
QDRANT__SERVICE__API_KEY in its environment (null when unset) to ${FAKE_STATE}/docker-env.log; rmp-qdrant exists
while ${FAKE_STATE}/container does, and another container <name> while ${FAKE_STATE}/container-<name> does.
FAKE_DOCKER_FAIL=<run|stop> fails that command with docker's message."""
import json, os, sys
args, state = sys.argv[1:], os.environ["FAKE_STATE"]
verb = args[1] if args[0] == "container" else args[0]
name = args[-1] if verb in ("inspect", "stop", "rm", "logs") else "rmp-qdrant"
container = os.path.join(state, "container" if name == "rmp-qdrant" else "container-" + name)
with open(os.path.join(state, "docker.log"), "a") as log:
    log.write(json.dumps(args) + "\n")
with open(os.path.join(state, "docker-env.log"), "a") as log:
    log.write(json.dumps(os.environ.get("QDRANT__SERVICE__API_KEY")) + "\n")
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

# curl -sf http://127.0.0.1:6333/readyz: ready while the container exists. The header lines of a config read from stdin
# (-K -) go to curl-headers.log.
FAKE_CURL = r'''#!/bin/sh
printf '%s\n' "$*" >> "${FAKE_STATE}/curl.log"
if [ "$1" = "-K" ] && [ "$2" = "-" ]; then
  sed -n 's/^header = "\(.*\)"$/\1/p' >> "${FAKE_STATE}/curl-headers.log"
fi
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
    start takes its storage from RMP_DATA_DIR. A start that hard-codes the live storage is never executed. Nor does one
    run without QDRANT_API_KEY, which the start and ops/qdrant_curl.sh would otherwise read from /etc/rmp/rmp.env."""
    if script == START:
        assert re.search(r'^DATA_DIR="\$\{RMP_DATA_DIR:-', script.read_text(), re.M), f"{script} hard-codes its data"
    assert "QDRANT_API_KEY" in env, "without QDRANT_API_KEY, the key is read from /etc/rmp/rmp.env"
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
    env = {**os.environ, "RMP_DATA_DIR": str(data), "RMP_PYTHON": sys.executable,
           "PATH": f"{fake}:{os.environ['PATH']}", "FAKE_STATE": str(state)}

    def run(script, container, one_off=False, **extra):
        """``script`` with the container present or not, and the one-off restore container too; the fakes' logs start
        empty."""
        run_env = {**env, **extra}
        _runs_only_against_fakes(script, run_env, fake)
        for name in ("docker.log", "docker-env.log", "curl.log", "curl-headers.log", "sleep.log", "container",
                     f"container-{ONE_OFF}"):
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
                               "docker_env": staticmethod(lambda: [json.loads(line) for line in log("docker-env.log")]),
                               "exists": staticmethod(lambda: (state / "container").exists()),
                               "storage": data / "qdrant-server"})


def _run_argv(storage, keyed=False):
    """The docker run of rmp-qdrant: the flags the live container has, and with a key its variable's name alone."""
    return ["run", "-d", "--name", "rmp-qdrant", "--restart", "unless-stopped",
            "-p", "127.0.0.1:6333:6333", "-p", "127.0.0.1:6334:6334",
            "-v", f"{storage}:/qdrant/storage", "-e", "QDRANT__SERVICE__GRPC_PORT=6334",
            *(["-e", "QDRANT__SERVICE__API_KEY"] if keyed else []), "qdrant/qdrant:v1.17.0"]


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
    # Without a key (the suite's QDRANT_API_KEY is empty), docker gets none and curl sends none.
    assert qdrant.docker_env()[-1] is None
    assert qdrant.log("curl.log") == ["-sf http://127.0.0.1:6333/readyz"] and not qdrant.log("curl-headers.log")


def test_start_gives_the_container_the_key_by_name_and_sends_it_to_curl_on_stdin(qdrant):
    """With a key (QDRANT_API_KEY, resolved by qdrant_api_key()), docker run names QDRANT__SERVICE__API_KEY and takes
    its value from the environment, and the readiness wait sends it on curl's stdin: no command line holds it, and the
    output does not show it."""
    result = qdrant.run(START, container=True, QDRANT_API_KEY=f" {KEY}\n")
    assert result.returncode == 0, result.stderr
    calls = qdrant.docker()
    assert calls == [INSPECT_ONE_OFF, INSPECT, ["stop", "rmp-qdrant"], ["rm", "rmp-qdrant"],
                     _run_argv(qdrant.storage, keyed=True)]
    assert qdrant.docker_env()[-1] == KEY
    assert qdrant.log("curl.log") == ["-K - -sf http://127.0.0.1:6333/readyz"]
    assert qdrant.log("curl-headers.log") == [f"api-key: {KEY}"]
    assert not any(KEY in word for call in calls for word in call)
    assert not any(KEY in line for line in qdrant.log("curl.log"))
    assert KEY not in result.stdout + result.stderr


def test_a_key_that_cannot_be_read_fails_the_start_before_it_touches_the_container(qdrant, tmp_path):
    broken = tmp_path / "python-that-fails"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    result = qdrant.run(START, container=True, RMP_PYTHON=str(broken))
    assert result.returncode == 1 and "could not read Qdrant's API key; rmp-qdrant is left as it is" in result.stderr
    assert qdrant.docker() == [INSPECT_ONE_OFF] and qdrant.exists() and not qdrant.log("curl.log")


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
    assert re.findall(r"docker (?:run|create)\b[^\n]*?--name (\S+)", section) == ["rmp-qdrant-from-snapshot"]
    # The one-off is created first, which turns start_qdrant.sh's guard on, then rmp-qdrant is checked, then the one-off
    # starts: a start of rmp-qdrant through systemd before the create is seen by the check, and one after it is refused.
    # A docker run would start the one-off before any check.
    assert not re.search(r"docker run\b", section)
    # Its image is the start script's, read before the create: an image taken from rmp-qdrant fails the create when
    # that container is gone, and a retried create alone would leave the questions before it.
    image = at(re.escape(r'''image="$(sed -n 's/^IMAGE="\(.*\)"$/\1/p' /root/.openclaw/rmp/ops/start_qdrant.sh)"'''))[0]
    assert re.search(r'^IMAGE="qdrant/qdrant:[\w.-]+"$', START.read_text(), re.M) and "{{.Config.Image}}" not in section
    created = at(r"docker create --name rmp-qdrant-from-snapshot")[0]
    assert image < created and at(r'\n     "\$\{image\}" \\\n')[0] > created
    asked = at(r"systemctl is-active rmp-qdrant\n")[0]
    running = at(re.escape("docker inspect -f '{{.State.Running}}' rmp-qdrant\n"))[-1]
    started = at(r"docker start rmp-qdrant-from-snapshot")[0]
    assert stop_qdrant[0] < created < asked < started and created < running < started
    # What the questions accept, and the remedy for either answer, come before the one-off starts; every retry runs the
    # whole block, so the questions follow a create that succeeded.
    accepted = at(r"must print `inactive` or `failed`, and `docker inspect` `false`")[0]
    remedies = [at(r"run `systemctl stop rmp-qdrant`")[-1], at(r"run `docker stop rmp-qdrant`")[-1]]
    assert running < accepted < min(remedies) and max(remedies) < started
    assert len(at(r"run the whole (?:first )?block(?: of this step)? again")) == 3
    removed = at(r"docker rm rmp-qdrant-from-snapshot")[-1]
    start_qdrant = at(r"systemctl start rmp-qdrant(?![\w-])")
    assert stop_qdrant[-1] < removed < start_qdrant[0]
    start_timers = at(r"systemctl start " + re.escape(" ".join(TIMERS)) + "\n")[0]
    assert start_qdrant[-1] < start_timers
    # The timer check is the procedure's last command.
    check = at(re.escape("""systemctl list-timers 'rmp-*' --all --no-pager --no-legend | awk '{print ($1 == "-" ? """
                         """"NO NEXT: " : "next: ") $(NF-1)}'"""))
    assert check[-1] > start_timers and check[-1] == max(at(r"\b(?:systemctl|docker) "))


def test_the_runbook_keys_the_one_off_by_name_and_sends_calls_by_hand_through_qdrant_curl():
    """The one-off restore container joins Docker's default bridge like rmp-qdrant, so it gets the same key: its value
    from the shell's environment, read as the start script reads it, and only its name on docker's command line. The
    shell drops it before rmp-qdrant starts again, and nothing prints it. Every call to Qdrant by hand goes through
    ops/qdrant_curl.sh."""
    runbook = (REPO / "docs" / "runbooks" / "backup-restore.md").read_text()
    section = runbook.split("**Whole store from scratch", 1)[1].split("\n## ", 1)[0]
    exported = section.index('   export QDRANT__SERVICE__API_KEY="$(cd /root/.openclaw/rmp && venv/bin/python -c '
                             "'from app.memory.vector import qdrant_api_key; print(qdrant_api_key())')\"")
    created = section.index("docker create --name rmp-qdrant-from-snapshot \\\n")
    create = section[created:section.index("--force-snapshot\n", created)]
    assert exported < created and "\n     -e QDRANT__SERVICE__API_KEY \\\n" in create
    assert "QDRANT__SERVICE__API_KEY=" not in create
    assert re.findall(r"\$\{#?QDRANT__SERVICE__API_KEY\}", section) == ["${#QDRANT__SERVICE__API_KEY}"]
    removed = section.rindex("docker rm rmp-qdrant-from-snapshot")
    unset = section.index("\n   unset QDRANT__SERVICE__API_KEY\n")
    assert removed < unset < section.index("\n   systemctl start rmp-qdrant\n")
    lines = [line for block in re.findall(r"```bash\n(.*?)```", runbook, re.S) for line in block.splitlines()]
    by_hand = [line for line in lines if "curl" in line and re.search(r":633[34]\b", line)]
    assert by_hand and all("bash ops/qdrant_curl.sh " in line for line in by_hand)


def test_r148_cites_the_start_scripts_loopback_ports():
    """docs/VISION_CONFORMANCE.md's R-148 row gives start_qdrant.sh's -p flags as the evidence that Qdrant publishes
    only on loopback; the line it cites holds them."""
    row = next(line for line in (REPO / "docs" / "VISION_CONFORMANCE.md").read_text().splitlines()
               if line.startswith("| R-148 |"))
    cited = int(re.search(r"ops/start_qdrant\.sh:(\d+)", row).group(1))
    assert "-p 127.0.0.1:6333:6333 -p 127.0.0.1:6334:6334" in START.read_text().splitlines()[cited - 1]
