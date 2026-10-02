"""Stop Aura's in-flight OpenClaw runs for a task, through the gateway's ``sessions.abort``.

OpenClaw 2026.9.1 stops a run started through ``/hooks/agent`` by its session key; the run id
the hook returns is not in the gateway's abort registry (probed live 2026-09-30). The tool call
already in flight finishes, then the run ends with stop reason "aborted". ``clearQueued``
drops any turn queued behind it. Every session of the task that may still run is aborted:
Aura's first session, reworks, refinement and planner, and the evaluator's; sessions the
gateway's store shows as finished are skipped.

Calls go through a warm helper (``openclaw_gateway.mjs``: OpenClaw's own gateway SDK, loaded
once), and through the ``openclaw`` CLI when the helper cannot answer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from weakref import WeakKeyDictionary

from app.db.database import AsyncSessionLocal
from app.db.models import Event
from app.openclaw_sessions import session_statuses, task_run_session_keys

logger = logging.getLogger("rmp.openclaw_control")

ABORT_TIMEOUT_MS = 15000
# The CLI starts node and connects to the gateway; on a loaded host that took 52 s (Sep 30).
CLI_TIMEOUT_SEC = 120
CONCURRENT_ABORTS = 6
# A session in one of these states has no run left to stop.
FINISHED = frozenset({"done", "failed", "killed"})
HELPER_SCRIPT = Path(__file__).with_name("openclaw_gateway.mjs")
HELPER_READY_SEC = 10
HELPER_CALL_SEC = 20
_background: Set[asyncio.Task] = set()


class GatewayHelper:
    """The warm node process of ``openclaw_gateway.mjs``; started on demand, again after it exits."""

    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._starting: Optional[asyncio.Task] = None

    def _command(self) -> List[str]:
        node, cli = shutil.which("node"), shutil.which("openclaw")
        if not node or not cli:
            raise RuntimeError("node or openclaw is not on PATH")
        return [node, str(HELPER_SCRIPT), str(Path(os.path.realpath(cli)).parent)]

    async def _spawn(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            *self._command(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        )
        ready = asyncio.get_running_loop().create_future()
        reader = asyncio.get_running_loop().create_task(self._read(proc, ready))
        _background.add(reader)
        reader.add_done_callback(_background.discard)
        try:
            await asyncio.wait_for(ready, timeout=HELPER_READY_SEC)
        except BaseException:
            _kill_group(proc)
            raise
        self._proc = proc

    async def _read(self, proc: asyncio.subprocess.Process, ready: asyncio.Future) -> None:
        try:
            while line := await proc.stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get("ready"):
                    if not ready.done():
                        ready.set_result(True)
                    continue
                future = self._pending.pop(str(message.get("id")), None)
                if future is not None and not future.done():
                    future.set_result(message)
        finally:
            if not ready.done():
                ready.set_exception(RuntimeError("gateway helper exited before it was ready"))
            if self._proc is proc:
                self._proc = None
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(RuntimeError("gateway helper exited"))
                self._pending.clear()

    async def ensure(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        if self._starting is None or self._starting.done():
            self._starting = asyncio.get_running_loop().create_task(self._spawn())
        await asyncio.shield(self._starting)

    async def call(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        await self.ensure()
        proc, request_id = self._proc, uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            proc.stdin.write((json.dumps({"id": request_id, "method": method, "params": params,
                                          "timeoutMs": ABORT_TIMEOUT_MS}) + "\n").encode())
            await proc.stdin.drain()
            message = await asyncio.wait_for(future, timeout=HELPER_CALL_SEC)
        finally:
            self._pending.pop(request_id, None)
        if not message.get("ok"):
            raise RuntimeError(message.get("error") or "gateway call failed")
        return message.get("result") or {}

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            _kill_group(proc, signal.SIGTERM)
            await proc.wait()


_helpers: "WeakKeyDictionary[asyncio.AbstractEventLoop, GatewayHelper]" = WeakKeyDictionary()


def gateway_helper() -> GatewayHelper:
    loop = asyncio.get_running_loop()
    if loop not in _helpers:
        _helpers[loop] = GatewayHelper()
    return _helpers[loop]


def warm_up_gateway_helper() -> None:
    """Start the helper in the background (API startup), so the first stop does not wait for it."""
    task = asyncio.get_running_loop().create_task(gateway_helper().ensure())
    _background.add(task)

    def done(t: asyncio.Task) -> None:
        _background.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning("Gateway helper did not start; stops use the CLI: %s", t.exception())

    task.add_done_callback(done)


async def close_gateway_helper() -> None:
    helper = _helpers.pop(asyncio.get_running_loop(), None)
    if helper is not None:
        await helper.close()


def _kill_group(proc: asyncio.subprocess.Process, sig: int = signal.SIGKILL) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


async def abort_session(session_key: str) -> Dict[str, Any]:
    """One ``sessions.abort``: status "aborted", "no-active-run", "error" or "timeout"."""
    try:
        result = await gateway_helper().call("sessions.abort", {"key": session_key, "clearQueued": True})
        return {"key": session_key, "status": str(result.get("status") or "unknown")}
    except Exception as exc:
        logger.warning("Gateway helper could not abort %s (%s); using the CLI", session_key, str(exc)[:200])
    return await _abort_with_cli(session_key)


async def _abort_with_cli(session_key: str) -> Dict[str, Any]:
    cli = shutil.which("openclaw") or "openclaw"
    params = json.dumps({"key": session_key, "clearQueued": True})
    try:
        proc = await asyncio.create_subprocess_exec(
            cli, "gateway", "call", "sessions.abort", "--json", "--timeout", str(ABORT_TIMEOUT_MS),
            "--params", params, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return {"key": session_key, "status": "error", "error": str(exc)[:300]}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=CLI_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        # The CLI's node child would otherwise live on and send the abort later, unrecorded.
        _kill_group(proc)
        await proc.wait()
        return {"key": session_key, "status": "timeout"}
    if proc.returncode != 0:
        return {"key": session_key, "status": "error", "error": err.decode(errors="replace")[-300:]}
    try:
        data = json.loads(out.decode(errors="replace"))
    except ValueError:
        return {"key": session_key, "status": "error", "error": "unparseable gateway reply"}
    return {"key": session_key, "status": str(data.get("status") or "unknown")}


async def abort_task_runs(task_id: str, *, reason: str) -> List[Dict[str, Any]]:
    """Abort every session of the task that may still run, and record what the gateway answered.

    Aura's Claude sessions in the task end first: their turns are her tool calls.
    """
    from app.coding.direct import end_task_sessions

    try:
        await asyncio.to_thread(end_task_sessions, task_id, reason)
    except Exception as exc:
        logger.warning("Ending the Claude sessions of task %s failed: %s", task_id[:8], exc)
    keys = await asyncio.to_thread(task_run_session_keys, task_id)
    if not keys:
        return []
    statuses = await asyncio.to_thread(session_statuses, keys)
    finished = [k for k in keys if statuses.get(k) in FINISHED]
    limit = asyncio.Semaphore(CONCURRENT_ABORTS)

    async def one(key: str) -> Dict[str, Any]:
        async with limit:
            return await abort_session(key)

    results = list(await asyncio.gather(*(one(k) for k in keys if k not in finished)))
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
                                        "unconfirmed": [r["key"] for r in failed], "finished": finished}))
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
