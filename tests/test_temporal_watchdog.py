"""The Temporal watchdog recovers only when the probe reports Temporal unhealthy."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "ops" / "systemd" / "rmp-temporal-watchdog.service"
PROBE = REPO / "ops" / "temporal_healthcheck.py"


def _exec_start_command() -> str:
    line = next(l for l in UNIT.read_text().splitlines() if l.startswith("ExecStart="))
    match = re.fullmatch(r"ExecStart=/bin/bash -c '(.*)'", line)
    assert match, line
    return match.group(1).replace("$$", "$")  # systemd unescapes $$ before bash runs


@pytest.mark.parametrize("probe_rc, recovers", [(0, False), (1, True), (134, False), (2, False)])
def test_watchdog_runs_full_recovery_only_on_an_explicit_probe_failure(tmp_path, probe_rc, recovers):
    probe = tmp_path / "temporal_healthcheck.sh"
    probe.write_text(f"#!/bin/bash\nexit {probe_rc}\n")
    marker = tmp_path / "recovered"
    recover = tmp_path / "temporal_recover.sh"
    recover.write_text(f"#!/bin/bash\ntouch {marker}\nexit 0\n")
    for script in (probe, recover):
        script.chmod(0o755)
    command = (
        _exec_start_command()
        .replace("/root/.openclaw/rmp/ops/temporal_healthcheck.sh", str(probe))
        .replace("/root/.openclaw/rmp/ops/temporal_recover.sh", str(recover))
    )
    result = subprocess.run(["/bin/bash", "-c", command], capture_output=True, text=True)
    assert marker.exists() is recovers
    assert result.returncode == (0 if recovers else probe_rc)


FAKE_TEMPORALIO = '''
import atexit, os

class _Client:
    async def list_workflows(self, query):
        yield object()

class Client:
    @staticmethod
    async def connect(address):
        if os.environ.get("FAKE_TEMPORAL_DOWN"):
            raise ConnectionError("connection refused")
        # Stand-in for the SDK's native threads aborting interpreter finalization.
        atexit.register(os.abort)
        return _Client()
'''


def _run_probe(tmp_path, **env):
    pkg = tmp_path / "fake" / "temporalio"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "client.py").write_text(FAKE_TEMPORALIO)
    return subprocess.run(
        [sys.executable, str(PROBE)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(tmp_path / "fake"), **env},
        timeout=60,
    )


def test_probe_verdict_survives_a_crash_at_interpreter_exit(tmp_path):
    result = _run_probe(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "Temporal health: ok" in result.stdout


def test_probe_reports_an_unreachable_temporal(tmp_path):
    result = _run_probe(tmp_path, FAKE_TEMPORAL_DOWN="1")
    assert result.returncode == 1
    assert "Temporal health FAIL: connect failed" in result.stderr
