"""RMP's own test run of a job checkout: the repository's declared commands, as aura-coder in hardened units.

The exit codes systemd records are the test evidence, not Claude's report. Output files are root-owned;
the counts parsed from them are informational, because the tests are code the job may change.

The shared venv for a repository is built by root from the trusted source's requirements only. When
a job changed its requirements, the job builds its own venv inside the sandbox instead.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.coding.units import BASE_PATH, CODE_ROOT, CODER_HOME, CODER_USER, RUNS_DIR, systemd_run_argv, unit_properties
from app.coding.workspace import Job

VENVS_DIR = CODE_ROOT / "venvs"
CACHE_DIR = CODE_ROOT / "cache"
TEST_TMP = CACHE_DIR / "tmp"
JOB_VENV = ".aura/venv"
TAIL_LINES = 60
_PYTEST = re.compile(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|deselected)\b")
_NODE = re.compile(r"^ℹ (tests|pass|fail|cancelled|skipped|todo) (\d+)", re.M)


def requirements_hash(path: Path) -> Optional[str]:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def ensure_shared_venv(repo: str, requirements: Path) -> Path:
    """The read-only venv for ``repo``, rebuilt when the trusted requirements change."""
    venv = VENVS_DIR / repo
    stamp = venv / ".requirements.sha256"
    wanted = requirements_hash(requirements)
    if stamp.is_file() and stamp.read_text().strip() == wanted:
        return venv
    # Built in place: a venv's scripts record its path. Without the stamp, jobs build their own venv.
    shutil.rmtree(venv, ignore_errors=True)
    subprocess.run(["python3", "-m", "venv", str(venv)], check=True, capture_output=True, timeout=300)
    subprocess.run([str(venv / "bin" / "pip"), "install", "-q", "--disable-pip-version-check", "-r", str(requirements)],
                   check=True, capture_output=True, timeout=1800)
    stamp.write_text(f"{wanted}\n")
    return venv


def seed_test_cache(owner: Optional[str] = CODER_USER, *, source: Path = Path("/tmp")) -> Path:
    """aura-coder's TMPDIR for tests, with the Temporal test server already downloaded."""
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    for server in source.glob("temporal-test-server-sdk-python-*"):
        target = TEST_TMP / server.name
        if not target.exists():
            shutil.copy2(server, target)
    if owner:
        for path in [CACHE_DIR, TEST_TMP, *TEST_TMP.glob("temporal-test-server-*")]:
            shutil.chown(path, owner, owner)
    return TEST_TMP


def summarize(output: str) -> Dict[str, int]:
    """Counts from pytest's last summary line and node's test report."""
    counts: Dict[str, int] = {}
    summary = next((line for line in reversed(output.splitlines()) if _PYTEST.search(line) and " in " in line), "")
    for number, word in _PYTEST.findall(summary):
        counts[word.rstrip("s") if word.startswith("error") else word] = int(number)
    for word, number in _NODE.findall(output):
        counts[f"node_{word}"] = int(number)
    return counts


def commands_for(job: Job, cfg: Dict[str, Any]) -> List[List[str]]:
    entry = cfg["repositories"][job.repo]
    checkout = Path(job.checkout)
    setup = [list(argv) for argv in entry.get("setup") or []]
    venv = VENVS_DIR / job.repo
    uses_venv = any("{venv}" in arg for argv in entry["tests"] for arg in argv)
    if uses_venv:
        stamp = venv / ".requirements.sha256"
        shared = stamp.is_file() and stamp.read_text().strip() == requirements_hash(checkout / "requirements.txt")
        if not shared:
            setup += [["python3", "-m", "venv", JOB_VENV],
                      [f"{JOB_VENV}/bin/pip", "install", "-q", "--disable-pip-version-check", "-r", "requirements.txt"]]
            venv = checkout / JOB_VENV
    tests = [[arg.replace("{venv}", str(venv)) for arg in argv] for argv in entry["tests"]]
    # systemd refuses a relative executable path; a bare name is still looked up on PATH.
    return [[str(checkout / argv[0]) if "/" in argv[0] and not argv[0].startswith("/") else argv[0], *argv[1:]]
            for argv in setup + tests]


def run_tests(job: Job, cfg: Dict[str, Any], *, attempt: int = 1) -> Dict[str, Any]:
    """Every setup and test command in order; setup failing stops the run."""
    checkout = Path(job.checkout)
    entry = cfg["repositories"][job.repo]
    commands = commands_for(job, cfg)
    n_setup = len(commands) - len(entry["tests"])
    out_dir = RUNS_DIR / job.task_id / f"verify-{attempt}"
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    results: List[Dict[str, Any]] = []
    started = time.monotonic()
    for index, argv in enumerate(commands):
        log, exit_file = out_dir / f"{index}.log", out_dir / f"{index}.exit"
        exit_file.unlink(missing_ok=True)
        props = unit_properties(writable=[checkout, CODER_HOME, CACHE_DIR], memory_max=cfg["memory_max"],
                                cpu_quota=cfg["cpu_quota"], tasks_max=int(cfg["tasks_max"]),
                                runtime_max_sec=int(cfg["verify_timeout_sec"]))
        props += [f"StandardOutput=file:{log}", "StandardError=inherit",
                  f"ExecStopPost=+/bin/sh -c 'echo \"$SERVICE_RESULT $EXIT_CODE $EXIT_STATUS\" > {exit_file}'"]
        env = {"TMPDIR": str(TEST_TMP), "PATH": f"{checkout / JOB_VENV / 'bin'}:{BASE_PATH}", "CI": "1"}
        unit = f"aura-verify-{job.task_id}-{attempt}-{index}"
        tic = time.monotonic()
        subprocess.run(systemd_run_argv(unit, argv, properties=props, workdir=checkout, env=env, wait=True),
                       capture_output=True, text=True, timeout=int(cfg["verify_timeout_sec"]) + 120, stdin=subprocess.DEVNULL)
        output = log.read_text(errors="replace") if log.exists() else ""
        exit_line = exit_file.read_text().strip() if exit_file.exists() else "missing"
        results.append({"command": argv, "setup": index < n_setup, "exit": exit_line,
                        "ok": exit_line == "success exited 0", "seconds": round(time.monotonic() - tic, 1),
                        "counts": summarize(output), "tail": "\n".join(output.splitlines()[-TAIL_LINES:])})
        if index < n_setup and not results[-1]["ok"]:
            break
    tests = [r for r in results if not r["setup"]]
    record = {"ok": len(tests) == len(entry["tests"]) and all(r["ok"] for r in results),
              "commands": results, "seconds": round(time.monotonic() - started, 1)}
    (out_dir / "result.json").write_text(json.dumps(record, indent=2) + "\n")
    return record
