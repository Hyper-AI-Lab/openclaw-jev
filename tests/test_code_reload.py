"""Idle-aware runtime reload: restart only when no user task is active."""
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "reload_runtime_on_code_change.sh"


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run(tmp_path: Path, counts: list[str], *, max_defer: int = 3) -> tuple[Path, Path]:
    """Run the reload script with stubbed python/systemctl/curl; return (log, systemctl calls)."""
    root = tmp_path / "rmp"
    (root / "venv" / "bin").mkdir(parents=True)
    queue = tmp_path / "counts"
    queue.write_text("\n".join(counts) + "\n")
    # Each call pops one answer; "fail" simulates a broken task lookup.
    _executable(root / "venv" / "bin" / "python", f"""
next=$(head -n1 {queue}); sed -i '1d' {queue}
[[ -z "$next" ]] && next=$(tail -n1 <<< "{counts[-1]}")
[[ "$next" == fail ]] && exit 1
echo "$next"
""")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "systemctl.calls"
    _executable(bin_dir / "systemctl", f'echo "$*" >> {calls}\n')
    _executable(bin_dir / "curl", "exit 0\n")
    log = tmp_path / "reload.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RMP_ROOT": str(root),
        "RMP_CODE_RELOAD_LOCK": str(tmp_path / "reload.lock"),
        "RMP_CODE_RELOAD_LOG": str(log),
        "RMP_CODE_RELOAD_DEBOUNCE_SEC": "0",
        "RMP_CODE_RELOAD_POLL_SEC": "1",
        "RMP_CODE_RELOAD_MAX_DEFER_SEC": str(max_defer),
    }
    subprocess.run(["bash", str(SCRIPT)], env=env, check=True, timeout=30)
    return log, calls


def test_idle_restarts_immediately(tmp_path):
    log, calls = _run(tmp_path, ["0"])
    assert calls.read_text().split("\n")[0] == "restart rmp-api rmp-worker"
    assert "health OK" in log.read_text() and "deferring" not in log.read_text()


def test_busy_then_idle_restarts_once_after_waiting(tmp_path):
    log, calls = _run(tmp_path, ["2", "0"])
    text = log.read_text()
    assert "deferring restart: 2 active user task(s)" in text
    assert calls.read_text().strip() == "restart rmp-api rmp-worker"


@pytest.mark.parametrize("answer", ["1", "fail"])
def test_still_busy_or_unknown_leaves_restart_to_sentinel(tmp_path, answer):
    log, calls = _run(tmp_path, [answer], max_defer=2)
    assert not calls.exists()
    assert "the canary sentinel restarts stale runtimes once idle" in log.read_text()
    if answer == "fail":
        assert "deferring restart: unknown" in log.read_text()


def test_strict_count_raises_instead_of_reporting_idle(monkeypatch):
    import sqlalchemy

    from app.production import canary_sentinel

    def broken(*_a, **_k):
        raise RuntimeError("database down")

    monkeypatch.setattr(sqlalchemy, "create_engine", broken)
    assert canary_sentinel.count_active_user_tasks_sync() == 0
    with pytest.raises(RuntimeError):
        canary_sentinel.count_active_user_tasks_sync(strict=True)
