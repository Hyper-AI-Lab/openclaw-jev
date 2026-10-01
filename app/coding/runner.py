"""Claude Code runs for coding jobs: one transient systemd unit per run, as aura-coder.

The run's stream-json goes to a root-owned file (systemd opens it before dropping to the user), and
a root ``ExecStopPost`` records how the unit ended, so the run itself can write neither. The worker
tails the stream by offset and reattaches after a restart. A stop is recorded first, then sends
SIGINT (Claude Code ends its turn) and stops the unit, which kills the whole cgroup.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.coding import stream
from app.coding.units import CLAUDE_BIN, CODER_HOME, RUNS_DIR, TOKEN_ENV_FILE, systemd_run_argv, unit_properties

SYSTEMCTL = "systemctl"
USAGE_PROFILE = "claude_code:subscription"
STOP_GRACE_SEC = 10.0
# Past the grace period systemd escalates to SIGKILL after this long.
STOP_TIMEOUT_SEC = 15
# Resume a little after the limit resets.
RESUME_MARGIN_SEC = 60


@dataclass
class RunSpec:
    task_id: str
    number: int
    job_dir: Path
    prompt: str
    system_prompt: str = ""
    report_schema: Optional[Dict[str, Any]] = None
    resume_session: Optional[str] = None


@dataclass(frozen=True)
class Run:
    task_id: str
    number: int
    root: Path = RUNS_DIR

    @property
    def unit(self) -> str:
        return f"aura-claude-{self.task_id}-{self.number}"

    @property
    def dir(self) -> Path:
        return self.root / self.task_id / str(self.number)

    @property
    def stream_file(self) -> Path:
        return self.dir / "stream.jsonl"

    @property
    def exit_file(self) -> Path:
        return self.dir / "exit"

    @property
    def stop_file(self) -> Path:
        return self.dir / "stop"

    @property
    def usage_file(self) -> Path:
        return self.dir / "usage-recorded"


def claude_argv(spec: RunSpec, cfg: Dict[str, Any]) -> List[str]:
    argv = [str(CLAUDE_BIN), "-p", spec.prompt, "--output-format", "stream-json", "--verbose",
            "--model", cfg["model"], "--fallback-model", cfg["fallback_model"], "--max-turns", str(cfg["max_turns"]),
            "--permission-mode", "bypassPermissions", "--permission-prompts", "none"]
    if spec.system_prompt:
        argv += ["--append-system-prompt", spec.system_prompt]
    if spec.report_schema:
        argv += ["--json-schema", json.dumps(spec.report_schema, separators=(",", ":"))]
    if spec.resume_session:
        argv += ["--resume", spec.resume_session]
    return argv


def unit_active(unit: str) -> bool:
    return subprocess.run([SYSTEMCTL, "is-active", "--quiet", unit], capture_output=True).returncode == 0


def start(spec: RunSpec, cfg: Dict[str, Any], *, root: Path = RUNS_DIR) -> Run:
    """Start the run's unit, or return the run as it is when it already started (activity retries)."""
    run = Run(spec.task_id, spec.number, root)
    if unit_active(run.unit) or run.exit_file.exists():
        return run
    run.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in (run.stream_file, run.stop_file, run.usage_file):
        path.unlink(missing_ok=True)
    props = unit_properties(writable=[spec.job_dir, CODER_HOME], memory_max=cfg["memory_max"],
                            cpu_quota=cfg["cpu_quota"], tasks_max=int(cfg["tasks_max"]),
                            runtime_max_sec=int(cfg["run_timeout_sec"]), env_file=TOKEN_ENV_FILE)
    props += [
        f"StandardOutput=file:{run.stream_file}",
        f"StandardError=file:{run.dir / 'stderr.log'}",
        f"TimeoutStopSec={STOP_TIMEOUT_SEC}",
        f"ExecStopPost=+/bin/sh -c 'echo \"$SERVICE_RESULT $EXIT_CODE $EXIT_STATUS\" > {run.exit_file}'",
    ]
    argv = claude_argv(spec, cfg)
    meta = {"unit": run.unit, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "job_dir": str(spec.job_dir), "model": cfg["model"], "resume_session": spec.resume_session,
            "prompt_sha256": hashlib.sha256(spec.prompt.encode()).hexdigest(), "prompt_chars": len(spec.prompt)}
    (run.dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    subprocess.run(systemd_run_argv(run.unit, argv, properties=props, workdir=spec.job_dir),
                   check=True, capture_output=True, timeout=60)
    return run


def read_events(run: Run, offset: int) -> Tuple[List[str], int]:
    """Complete lines written after ``offset``, and the offset to read from next."""
    try:
        with run.stream_file.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
    except FileNotFoundError:
        return [], offset
    end = chunk.rfind(b"\n")
    if end < 0:
        return [], offset
    lines = chunk[:end].decode("utf-8", errors="replace").split("\n")
    return [line for line in lines if line.strip()], offset + end + 1


def exit_line(run: Run) -> Optional[str]:
    try:
        return run.exit_file.read_text().strip() or None
    except FileNotFoundError:
        return None


def status(run: Run) -> Dict[str, Any]:
    return {"active": unit_active(run.unit), "exit": exit_line(run), "stopped": run.stop_file.exists()}


def stop(run: Run, *, grace_sec: float = STOP_GRACE_SEC) -> Dict[str, Any]:
    """Recorded before any signal, so the outcome reads as stopped whatever the stream shows."""
    started = time.monotonic()
    if not run.stop_file.exists():
        run.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        run.stop_file.write_text(datetime.now(timezone.utc).isoformat(timespec="seconds") + "\n")
    was_active = unit_active(run.unit)
    if was_active:
        subprocess.run([SYSTEMCTL, "kill", "--signal=SIGINT", run.unit], capture_output=True)
        while unit_active(run.unit) and time.monotonic() - started < grace_sec:
            time.sleep(0.2)
        if unit_active(run.unit):
            subprocess.run([SYSTEMCTL, "stop", run.unit], capture_output=True, timeout=STOP_TIMEOUT_SEC + 30)
    return {"was_active": was_active, "active": unit_active(run.unit),
            "seconds": round(time.monotonic() - started, 1), "exit": exit_line(run)}


def finish(run: Run) -> stream.Outcome:
    lines, _ = read_events(run, 0)
    return stream.outcome(stream.parse_lines(lines), exit_line=exit_line(run), stopped=run.stop_file.exists())


def record_usage(run: Run, result: stream.Outcome) -> bool:
    """Book the run's tokens once, under ``claude_code``; False when there is nothing new to book.

    Only the result's ``usage`` counts: on a resumed session ``modelUsage`` and the cost also cover the
    earlier runs.
    """
    usage = result.usage or {}
    if run.usage_file.exists() or not usage.get("total_tokens"):
        return False
    from app.llm.usage_monitor import record_request

    prompt = usage["input_tokens"] + usage["cache_read_tokens"] + usage["cache_creation_tokens"]
    record_request(USAGE_PROFILE, "claude_code", input_tokens=prompt, output_tokens=usage["output_tokens"],
                   total_tokens=prompt + usage["output_tokens"], model=next(iter(usage.get("models") or {}), ""))
    run.usage_file.write_text(json.dumps(usage) + "\n")
    return True


def resume_at(result: stream.Outcome) -> Optional[int]:
    """When a run that hit the usage limit can resume its session (epoch seconds)."""
    if result.kind != "usage_limit" or not result.resets_at:
        return None
    return int(result.resets_at) + RESUME_MARGIN_SEC
