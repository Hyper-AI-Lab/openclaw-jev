"""RMP's own test runs of a job checkout, with fake units (tests/fakes/coding) standing in for aura-coder."""
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.coding import verify, workspace

FAKES = Path(__file__).resolve().parent / "fakes" / "coding"
TASK = "5d3c2a10-0000-4000-8000-00000000beef"
PASSING = "from app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n\ndef test_zero():\n    assert add(0, 0) == 0\n"


def sh_git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{FAKES}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_UNITS_DIR", str(tmp_path / "units"))
    for module, name, path in ((workspace, "JOBS_DIR", "jobs"), (workspace, "RUNS_DIR", "runs"),
                               (workspace, "REVIEW_DIR", "review"), (verify, "RUNS_DIR", "runs"),
                               (verify, "VENVS_DIR", "venvs"), (verify, "CACHE_DIR", "cache"),
                               (verify, "TEST_TMP", "cache/tmp")):
        (tmp_path / path).mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(module, name, tmp_path / path)
    source = tmp_path / "source"
    source.mkdir()
    sh_git("init", "-q", "-b", "main", cwd=source)
    (source / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (source / "test_app.py").write_text(PASSING)
    (source / "requirements.txt").write_text("")
    sh_git("add", "-A", cwd=source)
    sh_git("-c", "user.name=K", "-c", "user.email=k@example.com", "commit", "-q", "-m", "init", cwd=source)
    cfg = {"memory_max": "1G", "cpu_quota": "100%", "tasks_max": 64, "verify_timeout_sec": 120, "repositories": {"demo": {
        "remote": "Hyper-AI-Lab/demo", "source": str(source), "branch": "main", "deploy": "self", "setup": [],
        "tests": [[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_app.py"]]}}}
    job = workspace.prepare(TASK, "demo", "verify", cfg, owner=None)
    yield SimpleNamespace(cfg=cfg, job=job, checkout=Path(job.checkout), tmp=tmp_path)
    for pid_file in (tmp_path / "units").glob("*.pid"):
        try:
            os.killpg(int(pid_file.read_text()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


def test_a_passing_suite_is_evidence_from_its_exit_code(env):
    result = verify.run_tests(env.job, env.cfg)
    (command,) = result["commands"]
    assert result["ok"] and command["exit"] == "success exited 0" and command["counts"] == {"passed": 2}
    record = verify.RUNS_DIR / TASK / "verify-1"
    assert (record / "result.json").exists() and "2 passed" in (record / "0.log").read_text()


def test_a_failing_suite_fails_whatever_it_prints(env):
    (env.checkout / "app.py").write_text("def add(a, b):\n    print('2 passed in 0.01s')\n    return a - b\n")
    result = verify.run_tests(env.job, env.cfg, attempt=2)
    (command,) = result["commands"]
    assert not result["ok"] and command["exit"] == "exit-code exited 1"
    # add(0, 0) still holds with subtraction; the printed "2 passed" changes nothing.
    assert command["counts"] == {"failed": 1, "passed": 1} and "assert" in command["tail"]


def test_a_failed_setup_stops_before_the_tests(env):
    cfg = {**env.cfg, "repositories": {"demo": {**env.cfg["repositories"]["demo"], "setup": [["false"]]}}}
    result = verify.run_tests(env.job, cfg)
    assert not result["ok"] and [c["setup"] for c in result["commands"]] == [True]


def test_the_shared_venv_serves_only_jobs_with_the_same_requirements(env):
    cfg = {**env.cfg, "repositories": {"demo": {**env.cfg["repositories"]["demo"],
                                                 "tests": [["{venv}/bin/python", "-m", "pytest", "-q"]]}}}
    shared = verify.VENVS_DIR / "demo"
    shared.mkdir()
    (shared / ".requirements.sha256").write_text(verify.requirements_hash(env.checkout / "requirements.txt") + "\n")
    assert verify.commands_for(env.job, cfg) == [[f"{shared}/bin/python", "-m", "pytest", "-q"]]
    (env.checkout / "requirements.txt").write_text("requests\n")
    commands = verify.commands_for(env.job, cfg)
    local = env.checkout / verify.JOB_VENV
    assert commands[0] == ["python3", "-m", "venv", verify.JOB_VENV] and "requirements.txt" in commands[1]
    assert commands[-1] == [f"{local}/bin/python", "-m", "pytest", "-q"]


def test_the_shared_venv_is_rebuilt_only_when_the_trusted_requirements_change(env):
    requirements = env.tmp / "requirements.txt"
    requirements.write_text("")
    venv = verify.ensure_shared_venv("demo", requirements)
    assert (venv / "bin" / "python").exists()
    stamp = (venv / ".requirements.sha256").stat().st_mtime_ns
    assert verify.ensure_shared_venv("demo", requirements) == venv and (venv / ".requirements.sha256").stat().st_mtime_ns == stamp
    requirements.write_text("# changed\n")
    verify.ensure_shared_venv("demo", requirements)
    assert (venv / ".requirements.sha256").read_text().strip() == verify.requirements_hash(requirements)


def test_the_temporal_test_server_is_seeded_into_the_test_tmpdir(env):
    downloads = env.tmp / "downloads"
    downloads.mkdir()
    (downloads / "temporal-test-server-sdk-python-1.23.0").write_bytes(b"binary")
    tmp = verify.seed_test_cache(owner=None, source=downloads)
    assert (tmp / "temporal-test-server-sdk-python-1.23.0").read_bytes() == b"binary"


def test_pytest_and_node_summaries_are_counted():
    assert verify.summarize("..F\n=== 1 failed, 2 passed, 3 skipped, 1 error in 4.20s ===\n") == {
        "failed": 1, "passed": 2, "skipped": 3, "error": 1}
    assert verify.summarize("ℹ tests 22\nℹ pass 21\nℹ fail 1\n") == {"node_tests": 22, "node_pass": 21, "node_fail": 1}
    assert verify.summarize("nothing to see") == {}
