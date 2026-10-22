"""ops/notify_unit_failure.py, the alert a failed unit's OnFailure= starts (TRI-026).

notify_ops_slack and send_alert are patched, so nothing sends a DM or posts an alert, and the backup root is in
tmp_path. The keys are the ones notify_ops_slack sends each DM under (slack_idempotency_key of ``ops:<incident id>``
and the text): a DM whose key was sent before is not sent again.
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.activities.side_effects import slack_idempotency_key
from ops import notify_unit_failure as notify

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "notify_unit_failure.py"
BACKUP = "rmp-backup.service"
PROBE = "rmp-unit-failed-probe.service"
CHECK = "Check: systemctl status {0}; journalctl -u {0}"


class Sent:
    """The patched notify_ops_slack and send_alert, with every call. The DM returns ``delivered``, or raises it when it
    is an exception."""

    def __init__(self):
        self.delivered = True
        self.dms, self.alerts = [], []

    async def dm(self, text, *, incident_id):
        self.dms.append((text, incident_id))
        if isinstance(self.delivered, Exception):
            raise self.delivered
        return self.delivered

    async def alert(self, event_type, text, **kwargs):
        self.alerts.append((event_type, text, kwargs))
        return False


@pytest.fixture
def sent(monkeypatch, tmp_path):
    monkeypatch.setenv("RMP_BACKUP_ROOT", str(tmp_path / "backups"))
    record = Sent()
    monkeypatch.setattr(notify, "notify_ops_slack", record.dm)
    monkeypatch.setattr(notify, "send_alert", record.alert)
    return record


def utc(day, hour, minute, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=timezone.utc)


def backup(tmp_path, stamp, failures=None):
    """A backup directory in the backup root; with ``failures``, a manifest that lists them."""
    run = tmp_path / "backups" / stamp
    run.mkdir(parents=True)
    if failures is not None:
        (run / "manifest.json").write_text(json.dumps({"format": 2, "failures": failures}))
    return run


def key(sent, unit, now):
    """Run the script for ``unit`` at ``now``; the key its DM went under."""
    assert notify.main([unit], now=now) == 0
    text, incident_id = sent.dms[-1]
    return slack_idempotency_key(f"ops:{incident_id}", text)


def test_a_fresh_backup_with_failures_names_them(sent, tmp_path):
    backup(tmp_path, "20261021T031502Z", [])
    backup(tmp_path, "20261022T031502Z", ["qdrant", "temporal:temporal"])
    assert notify.main([BACKUP], now=utc(22, 3, 40)) == 0
    text = ("⚠️ rmp-backup.service failed (2026-10-22 UTC)\n"
            "Backup 20261022T031502Z: not backed up: qdrant, temporal:temporal\n" + CHECK.format(BACKUP))
    assert sent.dms == [(text, "unit-failed:rmp-backup.service:2026-10-22")]
    assert sent.alerts == [("unit.failed", text, {"severity": "error", "context": {"unit": BACKUP}})]


@pytest.mark.parametrize("manifest", [None, '{"format": 2, "failures": ["qd'], ids=["missing", "unparsable"])
def test_a_fresh_backup_directory_without_a_manifest_says_the_run_stopped_early(sent, tmp_path, manifest):
    """A manifest that does not parse counts as none, and the DM still goes out."""
    backup(tmp_path, "20261021T031502Z", [])
    run = backup(tmp_path, "20261022T031502Z")
    if manifest is not None:
        (run / "manifest.json").write_text(manifest)
    assert notify.main([BACKUP], now=utc(22, 4, 15)) == 0
    assert sent.dms[0][0].splitlines()[1:] == [
        "Backup 20261022T031502Z: the run left no manifest (it stopped before the end)", CHECK.format(BACKUP)]


def test_a_fresh_backup_whose_manifest_lists_no_failure_says_the_run_failed_after_writing_it(sent, tmp_path):
    backup(tmp_path, "20261022T031502Z", [])
    assert notify.main([BACKUP], now=utc(22, 3, 20)) == 0
    assert sent.dms[0][0].splitlines()[1] == (
        "Backup 20261022T031502Z: its manifest lists no failure (the run failed after writing it)")


def test_a_three_day_old_backup_directory_is_not_reported(sent, tmp_path):
    """Nor is a fresher directory not named like a stamp, and a missing backup root is no run either."""
    assert notify.main([BACKUP], now=utc(22, 3, 40)) == 0
    backup(tmp_path, "20261019T031502Z", ["qdrant"])
    (tmp_path / "backups" / "openclaw-state").mkdir()
    assert notify.main([BACKUP], now=utc(22, 3, 40)) == 0
    # Two hours after its stamp, a run is no longer the one that just failed; a second earlier, it still is.
    backup(tmp_path, "20261022T013500Z", ["qdrant"])
    assert notify.main([BACKUP], now=utc(22, 3, 35)) == 0
    for text, _ in sent.dms:
        assert text.splitlines()[1:] == ["no backup directory from this run", CHECK.format(BACKUP)]
    assert notify.main([BACKUP], now=utc(22, 3, 34, 59)) == 0
    assert sent.dms[-1][0].splitlines()[1] == "Backup 20261022T013500Z: not backed up: qdrant"


def test_the_incident_id_holds_the_unit_and_the_date_and_the_text_no_time_of_day(sent):
    assert notify.main([PROBE], now=datetime(2026, 10, 22, 13, 47, 9, tzinfo=timezone.utc)) == 0
    text = f"⚠️ {PROBE} failed (2026-10-22 UTC)\n" + CHECK.format(PROBE)
    assert sent.dms == [(text, f"unit-failed:{PROBE}:2026-10-22")]
    assert not re.search(r"\d:\d|13|47", text)
    assert sent.alerts == [("unit.failed", text, {"severity": "error", "context": {"unit": PROBE}})]


def test_two_failures_of_a_unit_on_one_utc_day_send_one_dm(sent):
    first, last = key(sent, PROBE, utc(22, 0, 5)), key(sent, PROBE, utc(22, 23, 55))
    assert first == last
    assert key(sent, PROBE, utc(23, 0, 5)) != first


def test_each_failed_backup_run_sends_its_own_dm_and_a_repeat_sends_none(sent, tmp_path):
    backup(tmp_path, "20261022T031502Z", ["qdrant"])
    first = key(sent, BACKUP, utc(22, 3, 40))
    assert key(sent, BACKUP, utc(22, 4, 30)) == first
    # A manual re-run the same day that fails the same way is another run.
    backup(tmp_path, "20261022T100003Z", ["qdrant"])
    assert key(sent, BACKUP, utc(22, 10, 20)) != first
    assert "Backup 20261022T100003Z: not backed up: qdrant" in sent.dms[-1][0]


@pytest.mark.parametrize("delivered", [False, RuntimeError("the receipts database is down")])
def test_an_undelivered_dm_exits_1(sent, delivered, capsys):
    """Its unit then fails, so systemctl --failed shows the lost alert. The webhook alert goes out all the same."""
    sent.delivered = delivered
    assert notify.main([PROBE], now=utc(22, 3, 40)) == 1
    assert len(sent.dms) == len(sent.alerts) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "the DM did not go out"


def test_the_script_runs_as_its_unit_starts_it_and_fails_when_no_dm_can_go_out(tmp_path):
    """The template's command, with the suite's environment: its settings and OpenClaw config name no Slack token, so
    the real notify_ops_slack sends nothing, and the exit code says so."""
    run = subprocess.run([sys.executable, str(SCRIPT), PROBE], cwd=tmp_path, capture_output=True, text=True,
                         timeout=120)
    assert run.returncode == 1, run.stderr
    assert run.stdout.splitlines()[-1] == "the DM did not go out"
