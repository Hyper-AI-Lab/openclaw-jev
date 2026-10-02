"""Aura's direct Claude sessions: she talks to Claude Code turn by turn, in any task, as root.

A session belongs to a task and has its own workspace under ``DIRECT_DIR``: a clone of her repository
from GitHub or an empty folder, never the live checkout. Each turn is ``claude -p`` in its own transient
unit (``--session-id`` the first time, ``--resume`` after) in Claude Code's auto permission mode, under
the host's policy. As for coding runs, systemd writes the stream and the exit line, and
``app.coding.runner`` reads them, stops the unit and books the usage.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.coding import runner, stream
from app.coding.units import CLAUDE_BIN, CODE_ROOT, TOKEN_ENV_FILE

DIRECT_DIR = CODE_ROOT / "direct"
UNIT_PREFIX = "aura-direct-"
CONFIG_DIR = Path("/root/.claude")
# Aura's choice of workspace, and the folder it is in the session's directory.
WORKSPACES = {"repo": "repo", "scratch": "work"}
PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
MAX_WAIT_SEC = 55
# A message travels as one argument of claude -p; Linux caps one argument at 128 KiB.
MAX_MESSAGE_BYTES = 120_000
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ID = re.compile(_UUID)
_TASK_SESSION = re.compile(rf"agent:main:rmp_task_({_UUID})")

SYSTEM_PROMPT = """You are working for Aura, Kirill's AI assistant, who is talking to you directly during one of her tasks. \
You run as root on Kirill's production server, in Claude Code's auto permission mode.

- Your workspace is {workspace}. Work there.
- Never edit {live}: it is Aura's live code, and a change there goes live at once.
- A change to Aura's code goes through a pull request: commit on a branch named aura/<topic> in a clone of her \
repository, push it with `aura-github push`, open the pull request with `aura-github gh pr create` (a title and a \
body that says what changed and how you tested it), and tell Aura its URL. Never merge: once CI's test check \
passes, Aura merges and deploys it.
- For GitHub use only `aura-github`. Never push to main. Other repositories are off limits unless Aura tells you \
Kirill asked for it.
- Never print, log or commit a secret; read a token only inside the command that needs it.
- If something is unclear, ask Aura instead of guessing.
- End each reply with what you did, what you found and what is left."""


class SessionError(Exception):
    """A request a session can't take: unknown, ended, busy or over the limit."""


@dataclass(frozen=True)
class Turn(runner.Run):
    root: Path = DIRECT_DIR
    session_id: str = ""

    @property
    def unit(self) -> str:
        return f"{UNIT_PREFIX}{self.task_id}-{self.session_id[:8]}-{self.number}"

    @property
    def dir(self) -> Path:
        return self.root / self.task_id / self.session_id / "turns" / str(self.number)


def task_of(session_key: str) -> Optional[str]:
    """The task of one of Aura's task sessions (``agent:main:rmp_task_<id>``, with any suffix)."""
    match = _TASK_SESSION.match(session_key or "")
    return match.group(1) if match else None


def create(task_id: str, workspace: str, title: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    if workspace not in WORKSPACES:
        raise SessionError(f"workspace must be one of: {', '.join(WORKSPACES)}")
    DIRECT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    session_id = str(uuid.uuid4())
    home = DIRECT_DIR / task_id / session_id
    home.mkdir(mode=0o700, parents=True)
    work = home / WORKSPACES[workspace]
    try:
        if workspace == "repo":
            repo = cfg["repositories"]["rmp"]
            _clone(work, f"https://github.com/{repo['remote']}.git", repo["source"])
        else:
            work.mkdir(mode=0o700)
    except Exception:
        shutil.rmtree(home, ignore_errors=True)
        raise
    session = {"id": session_id, "task_id": task_id, "workspace": workspace, "path": str(work),
               "title": title.strip()[:200], "status": "open", "created_at": _now(), "turns": 0}
    _write(home / "session.json", session)
    return session


def load(session_id: str) -> Dict[str, Any]:
    return json.loads(_session_file(session_id).read_text())


def sessions(task_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Sessions oldest first, each with whether its last turn is running."""
    if task_id is not None and not _ID.fullmatch(task_id):
        return []
    found = []
    for path in DIRECT_DIR.glob(f"{task_id or '*'}/*/session.json"):
        session = json.loads(path.read_text())
        last = _turn(session, session["turns"]) if session["turns"] else None
        found.append({**session, "running": bool(last and runner.unit_active(last.unit))})
    return sorted(found, key=lambda s: s["created_at"])


def send(session_id: str, message: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Start the session's next turn with Aura's message; the reply is read with ``wait``."""
    if not message.strip():
        raise SessionError("the message is empty")
    if len(message.encode()) > MAX_MESSAGE_BYTES:
        raise SessionError(f"the message is over {MAX_MESSAGE_BYTES} bytes; put long material in a file in the workspace")
    path = _session_file(session_id)
    session = json.loads(path.read_text())
    if session["status"] != "open":
        raise SessionError("the session has ended")
    if session["turns"] and runner.unit_active(_turn(session, session["turns"]).unit):
        raise SessionError(f"turn {session['turns']} is still running")
    if len(running_units()) >= int(cfg["direct_max_running"]):
        raise SessionError("too many Claude turns are running; try again when one has finished")
    turn = _turn(session, session["turns"] + 1)
    resume = any(CONFIG_DIR.glob(f"projects/*/{session_id}.jsonl"))
    argv = [str(CLAUDE_BIN), "-p", message, "--output-format", "stream-json", "--verbose",
            "--model", cfg["model"], "--fallback-model", cfg["fallback_model"], "--max-turns", str(cfg["max_turns"]),
            "--permission-mode", "auto", "--append-system-prompt", system_prompt(session, cfg),
            *(["--resume", session_id] if resume else ["--session-id", session_id])]
    props = [
        f"EnvironmentFile={TOKEN_ENV_FILE}",
        f"StandardOutput=file:{turn.stream_file}",
        f"StandardError=file:{turn.dir / 'stderr.log'}",
        f"TimeoutStopSec={runner.STOP_TIMEOUT_SEC}",
        f"RuntimeMaxSec={int(cfg['direct_turn_timeout_sec'])}",
        f"MemoryMax={cfg['memory_max']}",
        f"TasksMax={cfg['tasks_max']}",
        f"CPUQuota={cfg['cpu_quota']}",
        "CPUWeight=50",
        "IOWeight=50",
        "Nice=5",
        f"ExecStopPost=+/bin/sh -c 'echo \"$SERVICE_RESULT $EXIT_CODE $EXIT_STATUS\" > {turn.exit_file}'",
    ]
    env = {"HOME": "/root", "PATH": PATH, "LANG": "C.UTF-8", "CLAUDE_CONFIG_DIR": str(CONFIG_DIR)}
    command = ["systemd-run", f"--unit={turn.unit}", f"--working-directory={session['path']}", "--collect", "--quiet",
               *[f"--setenv={key}={value}" for key, value in env.items()],
               *[f"--property={prop}" for prop in props], "--", *argv]
    turn.dir.mkdir(mode=0o700, parents=True)
    _write(turn.dir / "meta.json", {"unit": turn.unit, "started_at": _now(), "model": cfg["model"],
                                    "resume": resume, "message": message})
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=60)
    except Exception:
        shutil.rmtree(turn.dir, ignore_errors=True)
        raise
    session.update(turns=turn.number, updated_at=_now())
    _write(path, session)
    return {"session": session_id, "turn": turn.number}


def status(session_id: str, number: int) -> Dict[str, Any]:
    """Where a turn stands: Claude's reply once it is done, its latest progress while it works."""
    session = load(session_id)
    if not 1 <= number <= session["turns"]:
        raise SessionError(f"turn {number} does not exist")
    turn = _turn(session, number)
    # The exit line first: once it is there the stream is complete, while a stream read first may miss its end.
    exit_line = runner.exit_line(turn)
    if exit_line is None and not runner.unit_active(turn.unit):
        time.sleep(1.0)
        # systemd writes the exit line just after the unit stops; a unit gone without one was lost.
        exit_line = runner.exit_line(turn) or ("lost" if not runner.unit_active(turn.unit) else None)
    lines, _ = runner.read_events(turn, 0)
    state = stream.parse_lines(lines)
    result = stream.outcome(state, exit_line=exit_line, stopped=turn.stop_file.exists())
    done = result.kind != "running"
    if done:
        runner.record_usage(turn, result)
    return {
        "session": session_id, "turn": number, "done": done, "outcome": result.kind,
        "reply": ((state.result or {}).get("result") or state.last_text) if done else "",
        "error": result.error, "resets_at": result.resets_at,
        "progress": state.milestones[-10:], "files_edited": state.files_edited,
        "commands": [command[:300] for command in state.commands[-10:]],
        "denied": [denial.get("tool_name") for denial in state.permission_denials],
        "tokens": (result.usage or {}).get("total_tokens"),
    }


async def wait(session_id: str, number: int, wait_sec: float) -> Dict[str, Any]:
    """``status`` once the turn is done or after ``wait_sec`` (at most ``MAX_WAIT_SEC``), whichever is first."""
    deadline = time.monotonic() + max(0.0, min(float(wait_sec), MAX_WAIT_SEC))
    turn = _turn(await asyncio.to_thread(load, session_id), number)
    while (time.monotonic() < deadline and runner.exit_line(turn) is None
           and await asyncio.to_thread(runner.unit_active, turn.unit)):
        await asyncio.sleep(1.0)
    return await asyncio.to_thread(status, session_id, number)


def end(session_id: str, reason: str = "ended") -> Dict[str, Any]:
    """Stop the running turn, if any, and close the session; its records and workspace stay."""
    path = _session_file(session_id)
    session = json.loads(path.read_text())
    stopped = []
    last = _turn(session, session["turns"]) if session["turns"] else None
    if last and runner.unit_active(last.unit):
        runner.stop(last)
        stopped.append(last.unit)
    if session["status"] == "open":
        session.update(status="ended", ended_at=_now(), end_reason=reason)
        _write(path, session)
    return {**session, "stopped": stopped}


def end_task_sessions(task_id: str, reason: str) -> List[str]:
    """End every open session of a task; the units that were stopped."""
    stopped = []
    for session in sessions(task_id):
        if session["status"] == "open":
            stopped += end(session["id"], reason)["stopped"]
    return stopped


def running_units() -> List[str]:
    listed = subprocess.run(["systemctl", "list-units", "--plain", "--no-legend", "--state=active,activating",
                             f"{UNIT_PREFIX}*"], capture_output=True, text=True, timeout=30)
    return [line.split()[0] for line in listed.stdout.splitlines() if line.strip()]


def task_turn_running(task_id: str) -> bool:
    return any(unit.startswith(f"{UNIT_PREFIX}{task_id}-") for unit in running_units())


def prune(days: int, *, now: Optional[datetime] = None) -> List[str]:
    """Remove the workspaces of sessions ended more than ``days`` ago; their records stay."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    removed = []
    for path in DIRECT_DIR.glob("*/*/session.json"):
        session = json.loads(path.read_text())
        work = Path(session["path"])
        if session["status"] == "ended" and work.exists() and datetime.fromisoformat(session["ended_at"]) < cutoff:
            shutil.rmtree(work, ignore_errors=True)
            removed.append(session["id"])
    return removed


def system_prompt(session: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    return SYSTEM_PROMPT.format(workspace=session["path"], live=cfg["repositories"]["rmp"]["source"])


def _clone(target: Path, url: str, reference: str) -> None:
    # The live checkout lends its objects so the clone is quick; --dissociate leaves the clone on its own.
    _git("clone", "--quiet", "--reference-if-able", reference, "--dissociate", url, str(target), timeout=300)
    _git("-C", str(target), "config", "user.name", "Aura (Claude Code)")
    _git("-C", str(target), "config", "user.email", "aura-coder@aura.local")


def _git(*args: str, timeout: int = 60) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True, text=True, timeout=timeout,
                   env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _session_file(session_id: str) -> Path:
    found = list(DIRECT_DIR.glob(f"*/{session_id}/session.json")) if _ID.fullmatch(session_id or "") else []
    if not found:
        raise SessionError(f"no session {session_id}")
    return found[0]


def _turn(session: Dict[str, Any], number: int) -> Turn:
    return Turn(session["task_id"], number, DIRECT_DIR, session["id"])


def _write(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
