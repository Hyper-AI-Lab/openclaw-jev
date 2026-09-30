"""Stop Aura's in-flight OpenClaw runs for a task, through the gateway's ``sessions.abort``.

OpenClaw 2026.9.1 stops a run started through ``/hooks/agent`` by its session key; the run id
the hook returns is not in the gateway's abort registry (probed live 2026-09-30). The tool call
already in flight finishes, then the run ends with stop reason "aborted". ``clearQueued``
drops any turn queued behind it. Every session of the task is aborted: Aura's first session,
reworks, refinement and planner, and the evaluator's.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any, Dict, List, Set

from app.db.database import AsyncSessionLocal
from app.db.models import Event
from app.openclaw_sessions import task_run_session_keys

logger = logging.getLogger("rmp.openclaw_control")

ABORT_TIMEOUT_MS = 15000
CLI_TIMEOUT_SEC = 30
CONCURRENT_ABORTS = 6
_background: Set[asyncio.Task] = set()


async def abort_session(session_key: str) -> Dict[str, Any]:
    """One ``sessions.abort`` call: status "aborted", "no-active-run", "error" or "timeout"."""
    cli = shutil.which("openclaw") or "openclaw"
    params = json.dumps({"key": session_key, "clearQueued": True})
    try:
        proc = await asyncio.create_subprocess_exec(
            cli, "gateway", "call", "sessions.abort", "--json", "--timeout", str(ABORT_TIMEOUT_MS),
            "--params", params, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return {"key": session_key, "status": "error", "error": str(exc)[:300]}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=CLI_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        proc.kill()
        return {"key": session_key, "status": "timeout"}
    if proc.returncode != 0:
        return {"key": session_key, "status": "error", "error": err.decode(errors="replace")[-300:]}
    try:
        data = json.loads(out.decode(errors="replace"))
    except ValueError:
        return {"key": session_key, "status": "error", "error": "unparseable gateway reply"}
    return {"key": session_key, "status": str(data.get("status") or "unknown")}


async def abort_task_runs(task_id: str, *, reason: str) -> List[Dict[str, Any]]:
    """Abort every session of the task and record what the gateway answered."""
    keys = await asyncio.to_thread(task_run_session_keys, task_id)
    if not keys:
        return []
    limit = asyncio.Semaphore(CONCURRENT_ABORTS)

    async def one(key: str) -> Dict[str, Any]:
        async with limit:
            return await abort_session(key)

    results = list(await asyncio.gather(*(one(k) for k in keys)))
    aborted = [r["key"] for r in results if r["status"] == "aborted"]
    failed = [r for r in results if r["status"] in ("error", "timeout")]
    if failed:
        logger.warning("OpenClaw abort for task %s left %d session(s) unconfirmed: %s", task_id[:8], len(failed),
                       failed[:2])
    try:
        async with AsyncSessionLocal() as db:
            db.add(Event(correlation_id=task_id, entity_type="task", entity_id=task_id,
                         event_type="openclaw.aborted",
                         event_payload={"reason": reason, "sessions": len(keys), "aborted": aborted,
                                        "unconfirmed": [r["key"] for r in failed]}))
            await db.commit()
    except Exception as exc:
        logger.warning("Abort event not recorded for %s: %s", task_id[:8], exc)
    return results


def schedule_abort(task_id: str, *, reason: str) -> None:
    """Abort in the background: a stop or an intake decision never waits on the gateway."""
    task = asyncio.get_running_loop().create_task(abort_task_runs(task_id, reason=reason))
    _background.add(task)

    def done(t: asyncio.Task) -> None:
        _background.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning("OpenClaw abort for task %s failed: %s", task_id[:8], t.exception())

    task.add_done_callback(done)
