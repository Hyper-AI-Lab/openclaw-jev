"""What Claude did in a task, from RMP's own records: Aura's direct sessions and the coding job's runs.

The task's memory document and the evaluator's evidence are both built from these, so what RMP
judges with and what it remembers are the same. Texts are as recorded; callers redact secrets.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.coding import direct, runner, stream
from app.coding.units import RUNS_DIR

PR_LINK = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
COMMAND_CHARS = 300


def task_records(task_id: str) -> List[Dict[str, Any]]:
    """Each direct session of the task, then its coding job, as a conversation of turns."""
    return [*_sessions(task_id), *_coding_job(task_id)]


def has_records(task_id: str) -> bool:
    return bool(direct.sessions(task_id)) or bool(_run_numbers(RUNS_DIR / task_id))


def section_text(records: List[Dict[str, Any]], reply_chars: int = 600) -> str:
    """A digest for the task document: each conversation, and what each turn asked, did and answered."""
    lines = []
    for record in records:
        lines.append(f"{record['title']} ({record['where']}; {len(record['turns'])} turn(s); {record['status']})")
        for turn in record["turns"]:
            asked = f" Aura asked: {_clip(turn['message'], 300)}" if turn["message"] else ""
            lines.append(f"- Turn {turn['number']} ({_how(turn)}).{asked} Claude: {_clip(turn['reply'], reply_chars) or '(no reply)'}")
            if turn["files_edited"]:
                lines.append(f"  Files edited: {', '.join(turn['files_edited'][:20])}")
            if turn["prs"]:
                lines.append(f"  Pull requests: {', '.join(turn['prs'])}")
            if turn.get("tests_ok") is not None:
                lines.append(f"  RMP's tests: {'passed' if turn['tests_ok'] else 'failed'}")
    return "\n".join(lines)


def conversation_text(record: Dict[str, Any]) -> str:
    """One conversation in full, a heading per turn, for its own document."""
    parts = [f"# {record['title']}", f"{record['where']}. Started {record['started_at']}; {record['status']}."]
    for turn in record["turns"]:
        parts.append(f"## Turn {turn['number']} ({_how(turn)})")
        if turn["message"]:
            parts.append(f"Aura asked:\n\n{turn['message']}")
        parts.append(f"Claude answered:\n\n{turn['reply'] or '(no reply)'}")
        if turn["commands"]:
            parts.append("Commands:\n" + "\n".join(f"- {c}" for c in turn["commands"]))
        if turn["files_edited"]:
            parts.append("Files edited:\n" + "\n".join(f"- {f}" for f in turn["files_edited"]))
        if turn["prs"]:
            parts.append("Pull requests:\n" + "\n".join(f"- {p}" for p in turn["prs"]))
        if turn.get("tests_ok") is not None:
            parts.append(f"RMP's tests {'passed' if turn['tests_ok'] else 'failed'}.")
    return "\n\n".join(parts)


def _how(turn: Dict[str, Any]) -> str:
    """How a turn went and ran: "success; planning turn; claude-opus-5-5; effort high"."""
    bits = [turn["outcome"], "planning turn" if turn.get("plan") else "", ", ".join(turn.get("models") or []),
            f"effort {turn['effort']}" if turn.get("effort") else ""]
    return "; ".join(bit for bit in bits if bit)


def _sessions(task_id: str) -> List[Dict[str, Any]]:
    out = []
    for session in direct.sessions(task_id):
        turns = []
        for number in range(1, session["turns"] + 1):
            turn = direct._turn(session, number)
            meta = _read(turn.dir / "meta.json") or {}
            turns.append({"number": number, "at": meta.get("started_at"), "message": meta.get("message", ""),
                          "plan": bool(meta.get("plan")), "effort": meta.get("effort"), **_what_claude_did(turn)})
        where = "a clone of her repository" if session["workspace"] == "repo" else "a scratch folder"
        status = f"ended: {session.get('end_reason')}" if session["status"] == "ended" else "open"
        out.append({"kind": "session", "id": session["id"], "title": session["title"] or "Claude session",
                    "where": f"Direct session in {where}", "status": status,
                    "started_at": session["created_at"], "turns": turns})
    return out


def _coding_job(task_id: str) -> List[Dict[str, Any]]:
    root = RUNS_DIR / task_id
    numbers = _run_numbers(root)
    if not numbers:
        return []
    job = _read(root / "job.json") or {}
    brief = ((_read(root / "deploy.json") or {}).get("report") or {}).get("brief") or {}
    turns = []
    for number in numbers:
        run = runner.Run(task_id, number, RUNS_DIR)
        meta = _read(run.dir / "meta.json") or {}
        verify = _read(root / f"verify-{number}" / "result.json")
        turns.append({"number": number, "at": meta.get("started_at"),
                      "message": (brief.get("goal") or "") if number == 1 else "",
                      **_what_claude_did(run), "tests_ok": verify.get("ok") if verify else None})
    return [{"kind": "coding_job", "id": task_id, "title": brief.get("title") or "Coding job",
             "where": f"Coding job on {job.get('repo', 'her repository')}, branch {job.get('branch', '?')}",
             "status": "recorded", "started_at": job.get("created_at"), "turns": turns}]


def _what_claude_did(run: runner.Run) -> Dict[str, Any]:
    exit_line = runner.exit_line(run)
    lines, _ = runner.read_events(run, 0)
    state = stream.parse_lines(lines)
    result = stream.outcome(state, exit_line=exit_line, stopped=run.stop_file.exists())
    reply = (result.report or {}).get("summary") or (state.result or {}).get("result") or state.last_text or ""
    commands = [command[:COMMAND_CHARS] for command in state.commands]
    return {"outcome": result.kind, "reply": reply, "files_edited": state.files_edited, "commands": commands,
            "prs": sorted(set(PR_LINK.findall("\n".join([reply, *state.commands])))),
            "models": state.models, "tokens": (result.usage or {}).get("total_tokens")}


def _run_numbers(root: Path) -> List[int]:
    return sorted(int(p.name) for p in root.iterdir() if p.is_dir() and p.name.isdigit()) if root.is_dir() else []


def _read(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
