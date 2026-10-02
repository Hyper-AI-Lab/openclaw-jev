"""Prove the coding runner's host setup: isolation holds, and Claude Code answers on Kirill's plan.

1. Preconditions: the aura-coder user, the pinned Claude Code, the coding policy and the firewall.
2. Isolation probes in a hardened unit, as aura-coder: secrets, /root, this host's services, private
   networks and system paths must be out of reach; the job directory, its home, its own ephemeral
   test servers and Anthropic's API must work; Claude Code's policy directory holds only the
   coding policy.
3. With a token: a real ``claude -p`` in a hardened unit reads a file and returns its phrase. The
   configured model is tried first, then the fallback; the result says which the plan allows.

The outcome is recorded at $RMP_DATA_DIR/coding/claude_smoke.json for readiness.

    venv/bin/python ops/claude_code_smoke.py [--probes-only]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.coding import firewall  # noqa: E402
from app.coding.credentials import read_meta  # noqa: E402
from app.coding.units import (  # noqa: E402
    CLAUDE_BIN, CODER_HOME, CODER_USER, JOBS_DIR, MANAGED_SETTINGS, TOKEN_ENV_FILE, TOKEN_META_FILE,
    UNIT_PREFIX, systemd_run_argv, unit_properties,
)
from app.config import RMP_DATA_DIR, RMP_ROOT, get_coding_config  # noqa: E402

RECORD = Path(RMP_DATA_DIR) / "coding" / "claude_smoke.json"
EXPECT_ALLOWED = {"write job dir", "write home", "own ephemeral server", "connect api.anthropic.com:443",
                  "coding policy in place"}

PROBE = r'''
import hashlib, json, os, socket, sys
job, policy_sha = sys.argv[1], sys.argv[2]
results = {}
def attempt(name, fn):
    try:
        fn()
        results[name] = "allowed"
    except Exception as exc:
        results[name] = "denied: " + type(exc).__name__
def read(path):
    with open(path, "rb") as fh:
        fh.read(1)
def write(path):
    with open(path, "w") as fh:
        fh.write("x")
    os.remove(path)
def connect(host, port, timeout=4):
    with socket.create_connection((host, port), timeout=timeout):
        pass
def unix(path):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(3)
    try:
        s.connect(path)
    finally:
        s.close()
def other_processes():
    if not [p for p in os.listdir("/proc") if p.isdigit() and os.stat("/proc/" + p).st_uid != os.getuid()]:
        raise PermissionError("no other user's process is visible")
def own_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    with socket.create_connection(("127.0.0.1", srv.getsockname()[1]), timeout=3):
        pass
    srv.close()
def coding_policy():
    if sorted(os.listdir("/etc/claude-code")) != ["managed-settings.json"]:
        raise ValueError("other policy files are visible")
    with open("/etc/claude-code/managed-settings.json", "rb") as fh:
        if hashlib.sha256(fh.read()).hexdigest() != policy_sha:
            raise ValueError("not the coding policy")
attempt("coding policy in place", coding_policy)
for path in ("/etc/rmp/rmp.env", "/etc/openclaw/openclaw.env", "/etc/aura-coder/claude.env",
             "/root/.config/github_pat", "/root/.openclaw/openclaw.json", "/root/.openclaw/rmp/settings.json"):
    attempt("read " + path, lambda p=path: read(p))
attempt("list /root", lambda: os.listdir("/root"))
attempt("see other users' processes", other_processes)
attempt("read /proc/1/cmdline", lambda: read("/proc/1/cmdline"))
for port in (22, 5432, 6333, 7233, 8000, 8791, 9222, 18789):
    attempt("connect 127.0.0.1:%d" % port, lambda p=port: connect("127.0.0.1", p))
attempt("connect [::1]:18789", lambda: connect("::1", 18789))
attempt("connect 172.17.0.1:6333", lambda: connect("172.17.0.1", 6333))
attempt("connect 169.254.169.254:80", lambda: connect("169.254.169.254", 80))
for name, path in (("docker socket", "/run/docker.sock"), ("postgres socket", "/run/postgresql/.s.PGSQL.5432"),
                   ("postgres socket via /var/run", "/var/run/postgresql/.s.PGSQL.5432"),
                   ("dbus system bus", "/run/dbus/system_bus_socket"), ("snapd socket", "/run/snapd.socket"),
                   ("containerd socket", "/run/containerd/containerd.sock")):
    attempt(name, lambda p=path: unix(p))
for path in ("/srv/aura-code/venvs/.probe", "/etc/.aura-probe", "/usr/local/bin/.aura-probe",
             "/root/.openclaw/.aura-probe"):
    attempt("write " + os.path.dirname(path), lambda p=path: write(p))
attempt("write job dir", lambda: write(os.path.join(job, ".probe")))
attempt("write home", lambda: write(os.path.expanduser("~/.aura-probe")))
attempt("own ephemeral server", own_server)
attempt("connect api.anthropic.com:443", lambda: connect("api.anthropic.com", 443, 10))
print(json.dumps(results))
'''


def _coder_version() -> str:
    run = subprocess.run(["runuser", "-u", CODER_USER, "--", str(CLAUDE_BIN), "--version"],
                         capture_output=True, text=True, timeout=60, cwd=str(CODER_HOME),
                         env={"HOME": str(CODER_HOME), "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
    return run.stdout.split()[0] if run.returncode == 0 and run.stdout.split() else ""


def preconditions(cfg: dict) -> list:
    problems = []
    try:
        pwd.getpwnam(CODER_USER)
    except KeyError:
        return [f"user {CODER_USER} missing; run ops/setup_aura_coder.sh"]
    version = _coder_version()
    if version != cfg["claude_version"]:
        problems.append(f"Claude Code is {version or 'missing'}, settings pin {cfg['claude_version']}")
    repo_copy = Path(RMP_ROOT) / "ops" / "aura_coder" / "managed-settings.json"
    if not MANAGED_SETTINGS.is_file() or _sha(MANAGED_SETTINGS) != _sha(repo_copy):
        problems.append(f"{MANAGED_SETTINGS} is missing or differs from {repo_copy}")
    if not firewall.active():
        problems.append("firewall table inet aura_coder is not loaded")
    return problems


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _job_dir(tag: str) -> Path:
    job = JOBS_DIR / f"_smoke-{tag}"
    job.mkdir(mode=0o700)
    shutil.chown(job, CODER_USER, CODER_USER)
    return job


def _run_unit(name: str, command: list, job: Path, cfg: dict, *, token: bool, timeout: int) -> subprocess.CompletedProcess:
    props = unit_properties(writable=[job, CODER_HOME], memory_max=cfg["memory_max"], cpu_quota=cfg["cpu_quota"],
                            tasks_max=int(cfg["tasks_max"]), runtime_max_sec=timeout,
                            env_file=TOKEN_ENV_FILE if token else None)
    argv = systemd_run_argv(name, command, properties=props, workdir=job, wait=True, pipe=True)
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 30, stdin=subprocess.DEVNULL)


def isolation(cfg: dict, tag: str) -> dict:
    job = _job_dir(f"{tag}-probe")
    try:
        run = _run_unit(f"{UNIT_PREFIX}probe-{tag}", ["/usr/bin/python3", "-c", PROBE, str(job), _sha(MANAGED_SETTINGS)],
                        job, cfg, token=False, timeout=120)
        try:
            results = json.loads(run.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"ok": False, "error": (run.stderr or run.stdout)[-400:]}
        wrong = {k: v for k, v in results.items()
                 if (k in EXPECT_ALLOWED) != (v == "allowed")}
        return {"ok": not wrong, "results": results, "unexpected": wrong}
    finally:
        shutil.rmtree(job, ignore_errors=True)


def claude_call(cfg: dict, tag: str) -> dict:
    job = _job_dir(f"{tag}-claude")
    phrase = f"PELICAN-PARADE-{secrets.token_hex(3).upper()}"
    (job / "README.md").write_text(f"# Smoke check\n\nThe smoke phrase is {phrase}.\n", encoding="utf-8")
    shutil.chown(job / "README.md", CODER_USER, CODER_USER)
    tried = []
    try:
        for model in dict.fromkeys([cfg["model"], cfg["fallback_model"]]):
            started = time.monotonic()
            command = [str(CLAUDE_BIN), "-p", "Read README.md in the current directory and reply with the smoke "
                       "phrase it contains, and nothing else.", "--output-format", "stream-json", "--verbose",
                       "--max-turns", "4", "--model", model, "--permission-mode", "bypassPermissions"]
            run = _run_unit(f"{UNIT_PREFIX}smoke-{tag}-{len(tried)}", command, job, cfg, token=True, timeout=240)
            outcome = _read_stream(run.stdout, phrase)
            outcome.update(model=model, seconds=round(time.monotonic() - started, 1), exit=run.returncode)
            if not outcome["ok"] and not outcome.get("error"):
                outcome["error"] = (run.stderr or run.stdout)[-400:]
            tried.append(outcome)
            if outcome["ok"]:
                break
        best = next((t for t in tried if t["ok"]), None)
        return {"ok": best is not None, "model": best["model"] if best else None, "tried": tried}
    finally:
        shutil.rmtree(job, ignore_errors=True)


def _read_stream(stdout: str, phrase: str) -> dict:
    init, result, tools = {}, {}, []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            init = event
        elif event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    tools.append(block.get("name"))
        elif event.get("type") == "result":
            result = event
    text = str(result.get("result") or "")
    ok = result.get("subtype") == "success" and not result.get("is_error") and phrase in text
    return {
        "ok": ok,
        "session_model": init.get("model"),
        "permission_mode": init.get("permissionMode"),
        "mcp_servers": init.get("mcp_servers"),
        "tools_used": tools,
        "num_turns": result.get("num_turns"),
        "error": None if ok else (text[:300] or None),
    }


def main(probes_only: bool) -> int:
    if os.geteuid() != 0:
        print("run as root", file=sys.stderr)
        return 2
    cfg = get_coding_config()
    tag = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "version": cfg["claude_version"]}
    problems = preconditions(cfg)
    record["preconditions"] = problems
    record["isolation"] = isolation(cfg, tag) if not problems else {"ok": False, "error": "preconditions failed"}
    if probes_only or problems or not TOKEN_ENV_FILE.is_file():
        record["claude"] = {"ok": False, "error": "skipped" if probes_only else "no token or preconditions failed"}
    else:
        record["claude"] = claude_call(cfg, tag)
        record["token"] = {k: v for k, v in (read_meta(TOKEN_META_FILE) or {}).items() if k != "fingerprint"}
    record["ok"] = not problems and record["isolation"].get("ok") and (probes_only or record["claude"].get("ok"))
    RECORD.parent.mkdir(parents=True, exist_ok=True)
    RECORD.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    for problem in problems:
        print(f"FAIL: {problem}")
    iso = record["isolation"]
    print(f"isolation: {'ok' if iso.get('ok') else 'FAILED'}" + (f" unexpected={iso.get('unexpected')}" if iso.get("unexpected") else ""))
    claude = record["claude"]
    if claude.get("tried"):
        for attempt in claude["tried"]:
            print(f"claude {attempt['model']}: {'ok' if attempt['ok'] else 'failed'} in {attempt['seconds']} s"
                  f" (model {attempt.get('session_model')}, mode {attempt.get('permission_mode')},"
                  f" tools {attempt.get('tools_used')}){'' if attempt['ok'] else ' — ' + str(attempt.get('error'))[:200]}")
    else:
        print(f"claude: {claude.get('error')}")
    print(f"record: {RECORD}")
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--probes-only", action="store_true", help="skip the Claude Code call")
    sys.exit(main(parser.parse_args().probes_only))
