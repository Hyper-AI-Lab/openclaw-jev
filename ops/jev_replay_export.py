"""Export past Slack DM intakes as unlabeled Jev replay cases (read-only on the database).

The output holds Kirill's message text, so it belongs under data/ (git-ignored) and must never be
committed. Context is rebuilt as of each message: prior dialogue, tasks finished before it, and
tasks still running then. Memory hits cannot be rebuilt and are left empty.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import Task, TaskIntakeDecision, TaskMessage, TaskRegistryEntry
from app.notification_policy import is_internal_task
from app.task_registry.session_identity import dialogue_lookup_keys

ROOT = Path(__file__).resolve().parents[1]
_SMOKE = re.compile(r"^Intake \w+ smoke\b")
_GATE_RATIONALES = ("Duplicate intent",)


def _one_line(text: str, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


def _is_dm(row: TaskIntakeDecision) -> bool:
    session, intent = row.session_key or "", (row.intent_snippet or "").strip()
    kind = session.split(":")[2] if session.count(":") >= 2 else ""
    return bool(intent) and kind in ("main", "slack") and "canary" not in session.lower() \
        and not is_internal_task(intent, "", []) and not _SMOKE.match(intent) \
        and not (row.rationale or "").startswith(_GATE_RATIONALES)


async def _case(db, row: TaskIntakeDecision, n: int) -> dict:
    at: datetime = row.created_at
    intent = (row.intent_snippet or "").strip()
    keys = dialogue_lookup_keys(row.session_key or "") or [row.session_key]
    tasks = (await db.execute(
        select(Task).where(Task.openclaw_session_key.in_(keys), Task.task_type == "user", Task.created_at < at)
        .order_by(Task.created_at.desc()).limit(12)
    )).scalars().all()
    earlier = [t for t in tasks if (t.goal or "")[:500].strip() != intent[:500]
        and not is_internal_task(t.goal or "", t.task_type or "", [])]
    ended = {e.task_id: e for e in (await db.execute(
        select(TaskRegistryEntry).where(TaskRegistryEntry.task_id.in_([t.id for t in earlier] or [""]))
    )).scalars().all()}
    messages = (await db.execute(
        select(TaskMessage).where(TaskMessage.task_id.in_([t.id for t in earlier[:4]] or [""]),
            TaskMessage.role.in_(("user", "assistant")), TaskMessage.created_at < at)
        .order_by(TaskMessage.created_at.asc())
    )).scalars().all()
    dialogue = [f"{'Kirill' if m.role == 'user' else 'Aura'}: {_one_line(m.content, 400)}" for m in messages][-4:]
    running, finished = [], []
    for t in earlier:
        entry = ended.get(t.id)
        # Finished evidence mirrors production, which reads it from the registry only.
        if entry and entry.task_ended_at and entry.task_ended_at < at:
            if len(finished) < 5:
                finished.append({"request": _one_line(entry.intent_snippet or t.goal, 300),
                    "outcome": _one_line(entry.outcome_summary, 300), "result": entry.terminal_status or "",
                    "days_ago": round((at - entry.task_ended_at).total_seconds() / 86400, 2)})
            continue
        last_touch = entry.task_ended_at if entry and entry.task_ended_at else t.updated_at
        if last_touch and last_touch >= at and len(running) < 4:
            running.append({"goal": _one_line(t.goal, 300),
                "minutes_ago": max(1, int((at - t.created_at).total_seconds() // 60))})
    return {"id": f"replay-{n:03d}", "operation": "intake", "source": "replay", "message": intent,
        "dialogue": dialogue, "running": running, "finished": finished, "memory": [],
        "history": {"at": at.isoformat(), "decision": row.decision, "confidence": row.confidence,
            "rationale": _one_line(row.rationale, 160)},
        "expect": None, "label_status": "unlabeled"}


async def export() -> list[dict]:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(TaskIntakeDecision).order_by(TaskIntakeDecision.created_at))).scalars().all()
        seen, cases = set(), []
        for row in rows:
            key = (row.session_key, (row.intent_snippet or "").strip())
            if not _is_dm(row) or key in seen:
                continue
            seen.add(key)
            cases.append(await _case(db, row, len(cases) + 1))
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/jev_intake_replay.jsonl")
    args = parser.parse_args()
    if ROOT in args.output.resolve().parents and (ROOT / "data") not in args.output.resolve().parents:
        raise SystemExit("refusing to write private replay cases inside the repository outside data/")
    cases = asyncio.run(export())
    args.output.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases))
    print(json.dumps({"cases": len(cases), "output": str(args.output),
        "with_running": sum(bool(c["running"]) for c in cases),
        "with_finished": sum(bool(c["finished"]) for c in cases),
        "with_dialogue": sum(bool(c["dialogue"]) for c in cases)}))


if __name__ == "__main__":
    main()
