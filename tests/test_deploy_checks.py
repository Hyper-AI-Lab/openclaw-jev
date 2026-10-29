"""The checks after a deploy: RMP's health, the gateway, readiness compared like with like, and a canary.

Nothing here reaches a port or runs the canary: urlopen, the clock, the readiness report and canary.sh are fakes.
"""
import io
import json
from pathlib import Path

import pytest

from app.coding import deploy_checks

LIVE = Path("/nonexistent/live")


@pytest.fixture
def probes(monkeypatch):
    """``run``'s probes: RMP's health and the gateway answer as ``ok`` says (by URL), readiness reports ``report`` (an
    exception in it is raised), and the canary answers ``canary``."""
    seen = {"ok": {}, "report": {}, "canary": "ok", "waits": []}

    def wait_ok(url, seconds):
        seen["waits"].append((url, seconds))
        return seen["ok"].get(url, True)

    def readiness_report():
        if isinstance(seen["report"], Exception):
            raise seen["report"]
        return dict(seen["report"])

    monkeypatch.setattr(deploy_checks, "_wait_ok", wait_ok)
    monkeypatch.setattr(deploy_checks, "readiness_report", readiness_report)
    monkeypatch.setattr(deploy_checks, "canary", lambda live: seen["canary"])
    return seen


def test_readiness_is_reported_by_name_with_each_status(monkeypatch):
    report = {"checks": [{"name": "telemetry", "status": "pass"}, {"name": "backups", "status": "warn"},
                         {"name": "claude_direct", "status": "fail"}]}
    seen = []

    def urlopen(request, timeout):
        seen.append((request.full_url, request.get_header("X-rmp-api-key"), timeout))
        return io.BytesIO(json.dumps(report).encode())

    monkeypatch.setattr(deploy_checks.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr("app.config.get_api_key", lambda: "k")
    assert deploy_checks.readiness_report() == {"telemetry": "pass", "backups": "warn", "claude_direct": "fail"}
    assert seen == [("http://127.0.0.1:8000/api/production/readiness", "k", 90)]


def test_a_check_that_passed_and_fails_now_is_a_regression(probes):
    probes["report"] = {"telemetry": "fail", "slack": "pass"}
    assert deploy_checks.run(LIVE, ["rmp-api"], {"telemetry": "pass", "slack": "pass"}) == {
        "failure": "readiness checks failed: telemetry", "canary": None}


@pytest.mark.parametrize("before", [{}, {"backup_manifest": "warn"}], ids=["absent", "warn"])
def test_a_check_absent_or_warning_before_that_fails_now_is_new_failing(probes, before):
    probes["report"] = {"telemetry": "pass", "backup_manifest": "fail"}
    assert deploy_checks.run(LIVE, ["rmp-api"], {"telemetry": "pass", **before}) == {
        "failure": None, "canary": "ok", "new_failing": ["backup_manifest"]}


def test_a_check_failing_before_and_after_counts_for_nothing(probes):
    probes["report"] = {"telemetry": "fail"}
    assert deploy_checks.run(LIVE, ["rmp-api"], {"telemetry": "fail"}) == {
        "failure": None, "canary": "ok", "new_failing": []}


def test_ignored_checks_neither_fail_nor_are_reported(probes):
    probes["report"] = {"claude_direct": "fail", "backup_manifest": "fail"}
    assert deploy_checks.run(LIVE, ["rmp-api"], {"claude_direct": "pass"},
                             ignore=("claude_direct", "backup_manifest")) == {
        "failure": None, "canary": "ok", "new_failing": []}


def test_a_baseline_the_deploy_code_before_this_one_recorded_compares_as_it_did(probes):
    """That code recorded only the names failing before, as a list, in the run's state; a run it started and this code
    resumes treats every other check as passed then, so a check failing now and not then is a regression."""
    probes["report"] = {"telemetry": "fail", "backup_manifest": "fail", "slack": "pass"}
    assert deploy_checks.run(LIVE, ["rmp-api"], ["telemetry"]) == {
        "failure": "readiness checks failed: backup_manifest", "canary": None}
    assert deploy_checks.run(LIVE, ["rmp-api"], ["telemetry", "backup_manifest"]) == {
        "failure": None, "canary": "ok", "new_failing": []}
