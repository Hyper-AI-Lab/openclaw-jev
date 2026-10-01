"""The Claude Code runner against fake systemd-run, systemctl and claude (tests/fakes/coding)."""
import json
import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.coding import runner

FAKES = Path(__file__).resolve().parent / "fakes" / "coding"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude_streams"
CFG = {"model": "opus", "fallback_model": "sonnet", "max_turns": 50, "run_timeout_sec": 60,
       "memory_max": "1G", "cpu_quota": "100%", "tasks_max": 64}


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    units = tmp_path / "units"
    monkeypatch.setenv("PATH", f"{FAKES}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_UNITS_DIR", str(units))
    monkeypatch.setenv("FAKE_CLAUDE_ARGV", str(tmp_path / "argv.json"))
    monkeypatch.setattr(runner, "CLAUDE_BIN", FAKES / "claude")
    job = tmp_path / "job"
    job.mkdir()
    yield SimpleNamespace(root=tmp_path / "runs", job=job, argv=tmp_path / "argv.json", units=units)
    for pid_file in units.glob("*.pid"):
        try:
            os.killpg(int(pid_file.read_text()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


def spec(fakes, prompt, number=1, **kw):
    return runner.RunSpec(task_id="t-0001", number=number, job_dir=fakes.job, prompt=prompt, **kw)


def ended(run, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runner.exit_line(run) and not runner.unit_active(run.unit):
            return runner.exit_line(run)
        time.sleep(0.05)
    raise AssertionError(f"{run.unit} did not end")


def test_the_command_carries_the_settings_the_report_schema_and_the_resumed_session(fakes):
    argv = runner.claude_argv(spec(fakes, "Fix the bug.", system_prompt="Work only here.",
                                   report_schema={"type": "object"}, resume_session="sess-1"), CFG)
    assert argv[:3] == [str(runner.CLAUDE_BIN), "-p", "Fix the bug."]
    flags = {argv[i]: argv[i + 1] for i in range(3, len(argv) - 1)
             if argv[i].startswith("--") and not argv[i + 1].startswith("--")}
    assert flags["--output-format"] == "stream-json" and "--verbose" in argv
    assert (flags["--model"], flags["--fallback-model"], flags["--max-turns"]) == ("opus", "sonnet", "50")
    assert (flags["--permission-mode"], flags["--permission-prompts"]) == ("bypassPermissions", "none")
    assert flags["--append-system-prompt"] == "Work only here."
    assert json.loads(flags["--json-schema"]) == {"type": "object"} and flags["--resume"] == "sess-1"
    plain = runner.claude_argv(spec(fakes, "x"), CFG)
    assert not {"--append-system-prompt", "--json-schema", "--resume"} & set(plain)


def test_a_run_streams_to_its_file_and_ends_with_the_recorded_exit(fakes):
    run = runner.start(spec(fakes, "fixture:success_readonly"), CFG, root=fakes.root)
    assert ended(run) == "success exited 0"
    result = runner.finish(run)
    assert result.kind == "success" and result.report["phrase"] == "HERON-7"
    assert json.loads(fakes.argv.read_text())[:2] == ["-p", "fixture:success_readonly"]
    meta = json.loads((run.dir / "meta.json").read_text())
    assert meta["unit"] == "aura-claude-t-0001-1" and meta["prompt_chars"] == len("fixture:success_readonly")
    unit = json.loads((fakes.units / f"{run.unit}.json").read_text())
    assert unit["props"]["StandardOutput"] == f"file:{run.stream_file}"
    assert unit["props"]["ExecStopPost"].startswith("+/bin/sh -c ")
    assert unit["props"]["TimeoutStopSec"] == str(runner.STOP_TIMEOUT_SEC)


def test_reading_by_offset_reattaches_without_losing_or_repeating_a_line(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_DELAY", "0.05")
    expected = [line for line in (FIXTURES / "edit_and_test.jsonl").read_text().split("\n") if line.strip()]
    run = runner.start(spec(fakes, "fixture:edit_and_test"), CFG, root=fakes.root)
    seen, offset = [], 0
    while len(seen) < 3:
        lines, offset = runner.read_events(run, offset)
        seen += lines
        time.sleep(0.02)
    again = runner.Run("t-0001", 1, fakes.root)  # a restarted worker knows only the ids and the offset
    ended(again)
    lines, offset = runner.read_events(again, offset)
    assert seen + lines == expected
    assert runner.read_events(again, offset) == ([], offset)


def test_a_half_written_line_waits_for_its_newline(tmp_path):
    run = runner.Run("t-2", 1, tmp_path)
    run.dir.mkdir(parents=True)
    run.stream_file.write_bytes(b'{"type": "system"}\n{"type": "assis')
    lines, offset = runner.read_events(run, 0)
    assert lines == ['{"type": "system"}'] and offset == len(b'{"type": "system"}\n')
    with run.stream_file.open("ab") as fh:
        fh.write(b'tant"}\n')
    assert runner.read_events(run, offset)[0] == ['{"type": "assistant"}']


def test_starting_again_returns_the_same_run_while_it_runs_and_after_it_ended(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_DELAY", "0.1")
    first = runner.start(spec(fakes, "fixture:edit_and_test"), CFG, root=fakes.root)
    pid = (fakes.units / f"{first.unit}.pid").read_text()
    assert runner.start(spec(fakes, "fixture:edit_and_test"), CFG, root=fakes.root) == first
    assert (fakes.units / f"{first.unit}.pid").read_text() == pid
    ended(first)
    mtime = first.stream_file.stat().st_mtime_ns
    assert runner.start(spec(fakes, "fixture:edit_and_test"), CFG, root=fakes.root) == first
    assert first.stream_file.stat().st_mtime_ns == mtime


def test_a_stop_ends_the_run_within_seconds_and_reads_as_stopped(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_DELAY", "0.5")
    run = runner.start(spec(fakes, "fixture:interrupted"), CFG, root=fakes.root)
    time.sleep(0.6)
    done = runner.stop(run, grace_sec=5)
    assert done["was_active"] and not done["active"] and done["seconds"] < 5
    assert ended(run) == "success exited 0"
    assert runner.finish(run).kind == "stopped"


def test_a_run_that_ignores_sigint_is_stopped_with_its_whole_unit(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_DELAY", "0.5")
    run = runner.start(spec(fakes, "fixture:interrupted stubborn"), CFG, root=fakes.root)
    time.sleep(0.6)
    done = runner.stop(run, grace_sec=0.5)
    assert not done["active"] and ended(run) == "signal killed TERM"
    assert runner.finish(run).kind == "stopped"


def test_the_runtime_limit_reads_as_a_timeout(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_DELAY", "0.5")
    run = runner.start(spec(fakes, "fixture:edit_and_test"), {**CFG, "run_timeout_sec": 1}, root=fakes.root)
    assert ended(run).startswith("timeout")
    assert runner.finish(run).kind == "timeout"


@pytest.mark.parametrize("fixture, kind", [("max_turns", "max_turns"), ("auth_failure", "auth_failed"),
                                           ("unknown_model", "api_error"), ("synthetic_usage_limit", "usage_limit")])
def test_every_recorded_ending_is_classified(fakes, fixture, kind):
    run = runner.start(spec(fakes, f"fixture:{fixture}"), CFG, root=fakes.root)
    assert ended(run) == (FIXTURES / f"{fixture}.exit").read_text().strip()
    assert runner.finish(run).kind == kind


def test_usage_is_booked_once_under_claude_code(fakes, monkeypatch):
    from app.llm import usage_monitor

    calls = []
    monkeypatch.setattr(usage_monitor, "record_request", lambda *a, **kw: calls.append((a, kw)))
    assert "claude_code" in usage_monitor._SOURCES and "claude_code" not in usage_monitor.DIRECT_SOURCES
    run = runner.start(spec(fakes, "fixture:success_readonly"), CFG, root=fakes.root)
    ended(run)
    result = runner.finish(run)
    assert runner.record_usage(run, result) and not runner.record_usage(run, result)
    (args, kw), = calls
    assert args == (runner.USAGE_PROFILE, "claude_code") and kw["model"] == "claude-opus-5-5"
    usage = result.usage
    prompt = usage["input_tokens"] + usage["cache_read_tokens"] + usage["cache_creation_tokens"]
    assert (kw["input_tokens"], kw["output_tokens"]) == (prompt, usage["output_tokens"])


def test_a_resumed_run_books_only_its_own_tokens(fakes, monkeypatch):
    """modelUsage on a resumed session also covers the earlier runs."""
    from app.llm import usage_monitor

    calls = []
    monkeypatch.setattr(usage_monitor, "record_request", lambda *a, **kw: calls.append(kw))
    run = runner.start(spec(fakes, "fixture:resumed"), CFG, root=fakes.root)
    ended(run)
    result = runner.finish(run)
    assert runner.record_usage(run, result)
    session = result.usage["models"]["claude-opus-5-5"]
    assert calls[0]["output_tokens"] == result.usage["output_tokens"] < session["output_tokens"]


def test_a_usage_limit_resumes_its_session_after_the_reset(fakes):
    run = runner.start(spec(fakes, "fixture:synthetic_usage_limit"), CFG, root=fakes.root)
    ended(run)
    result = runner.finish(run)
    assert runner.resume_at(result) == result.resets_at + runner.RESUME_MARGIN_SEC
    ok = runner.start(spec(fakes, "fixture:success_readonly", number=2), CFG, root=fakes.root)
    ended(ok)
    assert runner.resume_at(runner.finish(ok)) is None
