"""ops/canary.sh ends on any terminal status of its task: completed is OK, every other terminal status fails at once,
and only a task still active after the last poll times out and is cancelled."""
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "canary.sh"
TASK_ID = "c0ffee00-0000-4000-8000-000000000001"


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run(tmp_path: Path, answer: dict):
    """Run the canary with every GET /tasks/{id} answering ``answer``. Returns (exit code, stdout, curl calls,
    health results written)."""
    tmp_path.mkdir(exist_ok=True)
    root = tmp_path / "rmp"
    (root / "venv" / "bin").mkdir(parents=True)
    results = tmp_path / "results.log"
    # The two skip checks print 0; write_health_canary_result logs the call it was given.
    _executable(root / "venv" / "bin" / "python", f"""
if [[ "$2" == *"write_health_canary_result("* ]]; then
  grep -F 'write_health_canary_result(' <<< "$2" >> {results}
else
  echo 0
fi
""")
    fakes = tmp_path / "bin"
    fakes.mkdir()
    calls = tmp_path / "curl.log"
    reply = tmp_path / "task.json"
    reply.write_text(json.dumps(answer))
    _executable(fakes / "curl", f"""
printf '%s\\n' "$*" >> {calls}
case "$*" in
  *"-X POST"*/cancel*) ;;
  *"-X POST"*) printf '{{"task_id": "{TASK_ID}"}}\\n200' ;;
  *) cat {reply} ;;
esac
""")
    _executable(fakes / "sleep", "exit 0\n")
    env = {
        **os.environ,
        "PATH": f"{fakes}:{os.environ['PATH']}",
        "RMP_ROOT": str(root),
        "RMP_API_KEY": "k",
        "RMP_CANARY_SKIP_SENTINEL": "1",
        "RMP_CANARY_MAX_POLLS": "3",
    }
    # The script must reach only the fakes, never this host's curl, sleep or RMP venv.
    assert shutil.which("curl", path=env["PATH"]) == str(fakes / "curl")
    assert shutil.which("sleep", path=env["PATH"]) == str(fakes / "sleep")
    python = Path(env["RMP_ROOT"]) / "venv" / "bin" / "python"
    assert python.is_file() and python.is_relative_to(tmp_path)
    run = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    written = results.read_text().splitlines() if results.exists() else []
    return run.returncode, run.stdout, calls.read_text().splitlines(), written


def _polls(calls):
    return [c for c in calls if c.endswith(f"/tasks/{TASK_ID}")]


def _cancels(calls):
    return [c for c in calls if "/cancel" in c]


def _result(status, error=""):
    return f"write_health_canary_result(status='{status}', task_id='{TASK_ID}', error='''{error}''')"


def test_canary_ends_on_a_terminal_status(tmp_path):
    code, out, calls, written = _run(tmp_path / "compensated", {"status": "compensated", "terminal": True})
    assert code == 1 and "CANARY FAIL: task compensated" in out
    assert written == [_result("failed", "task compensated")]
    assert len(_polls(calls)) == 1 and _cancels(calls) == []

    code, out, calls, written = _run(tmp_path / "completed", {"status": "completed", "terminal": True})
    assert code == 0 and "CANARY OK" in out
    assert written == [_result("completed")]
    assert _cancels(calls) == []

    code, out, calls, written = _run(tmp_path / "running", {"status": "running", "terminal": False})
    assert code == 1 and "CANARY TIMEOUT" in out
    assert written == [_result("timeout", "poll timeout")]
    assert len(_polls(calls)) == 3
    assert len(_cancels(calls)) == 1 and _cancels(calls)[0].endswith(f"/tasks/{TASK_ID}/cancel?reason=canary_timeout")


@pytest.mark.parametrize("status", ["cancelled", "stopped_by_user", "failed"])
def test_every_other_terminal_status_fails_at_once(tmp_path, status):
    code, out, calls, written = _run(tmp_path, {"status": status, "terminal": True})
    assert code == 1 and f"CANARY FAIL: task {status}" in out
    assert written == [_result("failed", f"task {status}")]
    assert len(_polls(calls)) == 1 and _cancels(calls) == []
