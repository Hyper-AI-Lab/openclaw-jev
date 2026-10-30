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

    # Without an API key the header is empty, and a report without checks has none.
    report = {"checks": None}
    monkeypatch.setattr("app.config.get_api_key", lambda: None)
    assert deploy_checks.readiness_report() == {} and seen[-1][1] == ""


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


@pytest.fixture
def canary_runs(monkeypatch):
    """canary.sh's runs: each answers the next of ``outputs`` (an exception in it is raised); each argv, cwd, timeout
    and the sentinel switch it was run with are recorded, and the waits between runs."""
    seen = {"outputs": [], "runs": [], "sleeps": []}

    def run(argv, *, cwd, capture_output, text, timeout, env):
        seen["runs"].append((argv, cwd, timeout, env.get("RMP_CANARY_SKIP_SENTINEL")))
        output = seen["outputs"].pop(0)
        if isinstance(output, BaseException):
            raise output
        return deploy_checks.subprocess.CompletedProcess(argv, 0 if "CANARY OK" in output else 1, stdout=output)

    monkeypatch.setattr(deploy_checks.subprocess, "run", run)
    monkeypatch.setattr(deploy_checks.time, "sleep", seen["sleeps"].append)
    return seen


@pytest.mark.parametrize("error, line", [
    (deploy_checks.subprocess.TimeoutExpired(["bash", "canary.sh"], 600),
     "CANARY ERROR: the canary did not finish (TimeoutExpired: Command '['bash', 'canary.sh']' timed out after 600 "
     "seconds)"),
    (FileNotFoundError(2, "No such file or directory"),
     "CANARY ERROR: the canary did not finish (FileNotFoundError: [Errno 2] No such file or directory)")])
def test_the_canary_returns_a_line_on_a_timeout(canary_runs, error, line):
    canary_runs["outputs"] = [error]
    assert deploy_checks.canary(LIVE) == line
    assert canary_runs["runs"] == [(["bash", str(LIVE / "ops" / "canary.sh")], LIVE, deploy_checks.CANARY_TIMEOUT_SEC,
                                    "1")]


def test_the_checks_never_raise(probes, monkeypatch):
    def broken(url, seconds):
        raise ValueError("unknown url type")

    monkeypatch.setattr(deploy_checks, "_wait_ok", broken)
    assert deploy_checks.run(LIVE, ["rmp-api"], {}) == {"failure": "the checks failed (unknown url type)",
                                                        "canary": None}
    probes["report"] = {"telemetry": "pass"}
    monkeypatch.setattr(deploy_checks, "_wait_ok", lambda url, seconds: True)
    monkeypatch.setattr(deploy_checks, "canary", lambda live: 1 / 0)
    assert deploy_checks.run(LIVE, ["rmp-api"], {}) == {"failure": "the checks failed (division by zero)",
                                                        "canary": None}


class Response(io.BytesIO):
    """urlopen's answer: a status and a body, usable as a context manager."""

    def __init__(self, status, body=b""):
        super().__init__(body)
        self.status = status


@pytest.mark.parametrize("answer, status", [
    (Response(200), 200),
    (deploy_checks.urllib.error.HTTPError("http://x", 503, "Service Unavailable", {}, None), 503),
    (ConnectionRefusedError(111, "Connection refused"), 0),
    (TimeoutError("timed out"), 0)])
def test_a_status_is_the_answers_code_or_0_when_nothing_answered(monkeypatch, answer, status):
    seen = []

    def urlopen(url, timeout):
        seen.append((url, timeout))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(deploy_checks.urllib.request, "urlopen", urlopen)
    assert deploy_checks._status("http://127.0.0.1:8000/health") == status
    assert seen == [("http://127.0.0.1:8000/health", 10)]


@pytest.fixture
def clock(monkeypatch):
    """A monotonic clock that a sleep moves on, and urlopen answering the statuses in ``statuses`` in turn (the last
    one again): 200 an answer, another status an HTTP error, 0 nothing listening."""
    seen = {"now": 1000.0, "statuses": [], "asked": [], "slept": []}

    def sleep(seconds):
        seen["slept"].append(seconds)
        seen["now"] += seconds

    def urlopen(url, timeout):
        seen["asked"].append(seen["now"])
        status = seen["statuses"].pop(0) if len(seen["statuses"]) > 1 else seen["statuses"][0]
        if status == 0:
            raise ConnectionRefusedError(111, "Connection refused")
        if status != 200:
            raise deploy_checks.urllib.error.HTTPError(url, status, "Service Unavailable", {}, None)
        return Response(status)

    monkeypatch.setattr(deploy_checks.time, "monotonic", lambda: seen["now"])
    monkeypatch.setattr(deploy_checks.time, "sleep", sleep)
    monkeypatch.setattr(deploy_checks.urllib.request, "urlopen", urlopen)
    return seen


def test_waiting_for_ok_polls_until_it_answers_200(clock):
    clock["statuses"] = [0, 503, 200]
    assert deploy_checks._wait_ok("http://127.0.0.1:8000/health", 120) is True
    assert clock["asked"] == [1000.0, 1002.0, 1004.0] and clock["slept"] == [2, 2]


def test_waiting_for_ok_gives_up_at_its_deadline(clock):
    clock["statuses"] = [0]
    assert deploy_checks._wait_ok("http://127.0.0.1:8000/health", 5) is False
    assert clock["asked"] == [1000.0, 1002.0, 1004.0]


def test_the_canary_passes(canary_runs):
    canary_runs["outputs"] = ["CANARY OK\n"]
    assert deploy_checks.canary(LIVE) == "ok" and len(canary_runs["runs"]) == 1 and canary_runs["sleeps"] == []


def test_a_skipped_canary_is_tried_again_and_then_passes(canary_runs):
    canary_runs["outputs"] = ["CANARY SKIP: 1 active user task(s) — deferring hourly canary\n", "CANARY OK\n"]
    assert deploy_checks.canary(LIVE) == "ok"
    assert len(canary_runs["runs"]) == 2 and canary_runs["sleeps"] == [deploy_checks.CANARY_WAIT_SEC]


def test_a_canary_skipped_every_time_is_skipped(canary_runs):
    canary_runs["outputs"] = ["CANARY SKIP: 1 active user task(s)\n"] * deploy_checks.CANARY_TRIES
    assert deploy_checks.canary(LIVE) == "skipped"
    assert len(canary_runs["runs"]) == deploy_checks.CANARY_TRIES
    assert canary_runs["sleeps"] == [deploy_checks.CANARY_WAIT_SEC] * deploy_checks.CANARY_TRIES


@pytest.mark.parametrize("output, line", [
    ("posting the task\nCANARY FAIL: task failed\nrunning the sentinel\n", "CANARY FAIL: task failed"),
    ("CANARY TIMEOUT\n", "CANARY TIMEOUT"),
    ("curl: (7) Failed to connect\n", "the canary exited 1")])
def test_a_failing_canary_returns_its_line(canary_runs, output, line):
    canary_runs["outputs"] = [output]
    assert deploy_checks.canary(LIVE) == line and len(canary_runs["runs"]) == 1


def test_rmp_that_does_not_come_back_fails_the_checks(probes):
    probes["ok"] = {"http://127.0.0.1:8000/health": False}
    assert deploy_checks.run(LIVE, ["rmp-api", "rmp-worker"], {}) == {
        "failure": "RMP's /health did not answer within 2 minutes", "canary": None}
    assert probes["waits"] == [("http://127.0.0.1:8000/health", 120)]


def test_the_gateway_is_awaited_only_when_it_was_restarted(probes):
    probes["ok"] = {deploy_checks.GATEWAY_READY: False}
    assert deploy_checks.run(LIVE, ["rmp-api"], {}) == {"failure": None, "canary": "ok", "new_failing": []}
    assert probes["waits"] == [("http://127.0.0.1:8000/health", 120)]
    assert deploy_checks.run(LIVE, ["openclaw-gateway"], {}) == {
        "failure": "the gateway was not ready within 5 minutes", "canary": None}
    assert probes["waits"][-1] == (deploy_checks.GATEWAY_READY, 300)


def test_a_readiness_report_that_fails_fails_the_checks(probes):
    probes["report"] = ConnectionResetError(104, "Connection reset by peer")
    assert deploy_checks.run(LIVE, ["rmp-api"], {}) == {
        "failure": "the readiness report failed ([Errno 104] Connection reset by peer)", "canary": None}


@pytest.mark.parametrize("canary, result", [
    ("ok", {"failure": None, "canary": "ok", "new_failing": []}),
    ("skipped", {"failure": None, "canary": "skipped", "new_failing": []}),
    ("CANARY FAIL: task failed", {"failure": "CANARY FAIL: task failed", "canary": None}),
    ("CANARY ERROR: the canary did not finish (TimeoutExpired: x)",
     {"failure": "CANARY ERROR: the canary did not finish (TimeoutExpired: x)", "canary": None})])
def test_the_canary_decides_once_readiness_passed(probes, canary, result):
    probes["report"], probes["canary"] = {"telemetry": "pass"}, canary
    assert deploy_checks.run(LIVE, ["rmp-api"], {"telemetry": "pass"}) == result
