"""Activities of coding tasks (``app/workflows/coding_task.py``).

The Claude Code run and RMP's verification are long. They heartbeat, so after a worker restart
Temporal retries them on the new worker, which reattaches to the run's still-running unit. Only a
cancel the workflow asked for (Kirill's stop) stops a unit: a worker shutting down or a missed
heartbeat leaves it running for the retry. Aura's review reads the diff from disk, so the diff never
enters the workflow's history.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from temporalio import activity

from app.coding import deploy, prompts, runner, stream, verify, workspace
from app.coding.units import RUNS_DIR
from app.telemetry import traced_activity

SLOT_FILE = RUNS_DIR / "coding-slot.json"
# "deploying": the deploy unit owns the task after the hand-off, and holds the slot until the report run ends.
ACTIVE_TASK_STATUSES = frozenset({"created", "running", "pending", "pending_user_input", "blocked", "needs_replan",
                                  "deploying"})
POLL_SEC = 2.0
LIVENESS_SEC = 180
NOTICE_SEC = 900
# A unit that is inactive with no exit record for this long is gone (it never started, or systemd lost it).
UNIT_GONE_SEC = 30
UNIT_KINDS = ("claude", "verify", "collect")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _context(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {key: payload.get(key) for key in ("task_id", "session_key", "intent", "task_type", "tags")}


async def _notify(payload: Dict[str, Any], message: str) -> None:
    from app.activities.openclaw_activities import notify_slack_user

    try:
        await notify_slack_user({**_context(payload), "message": message})
    except Exception as exc:
        activity.logger.warning("Coding notice failed for %s: %s", payload.get("task_id"), exc)


async def _touch(task_id: str) -> None:
    from app.activities.db_activities import touch_task_liveness

    try:
        await touch_task_liveness({"task_id": task_id})
    except Exception as exc:
        activity.logger.warning("Liveness touch failed for %s: %s", task_id, exc)


def _reply_text(response: Dict[str, Any]) -> str:
    try:
        return str(response["result"]["payloads"][0]["text"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


@contextmanager
def _slot_lock() -> Iterator[None]:
    SLOT_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(SLOT_FILE.with_suffix(".lock"), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def slot_holder() -> Optional[str]:
    try:
        return json.loads(SLOT_FILE.read_text()).get("task_id")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


async def _task_active(task_id: str) -> bool:
    from app.db.database import AsyncSessionLocal
    from app.db.models import Task

    async with AsyncSessionLocal() as db:
        task = await db.get(Task, task_id)
    return task is not None and task.status in ACTIVE_TASK_STATUSES


@traced_activity("coding.settings")
async def coding_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    from app.config import get_coding_config

    cfg = get_coding_config()
    return {"enabled": bool(cfg.get("enabled")), "max_rounds": int(cfg["max_rounds"]),
            "repositories": {name: {"remote": entry["remote"], "deploy": entry.get("deploy")}
                             for name, entry in cfg["repositories"].items()}}


@traced_activity("coding.acquire_slot")
async def acquire_coding_slot(payload: Dict[str, Any]) -> Dict[str, Any]:
    """One coding job at a time; a holder whose task has ended loses the slot."""
    task_id = payload["task_id"]
    holder = slot_holder()
    if holder and holder != task_id and await _task_active(holder):
        return {"granted": False, "holder": holder}
    with _slot_lock():
        current = slot_holder()
        if current not in (None, task_id, holder):
            return {"granted": False, "holder": current}
        SLOT_FILE.write_text(json.dumps({"task_id": task_id, "since": _now()}) + "\n")
    return {"granted": True, "holder": task_id}


@traced_activity("coding.release_slot")
async def release_coding_slot(payload: Dict[str, Any]) -> bool:
    with _slot_lock():
        if slot_holder() != payload["task_id"]:
            return False
        SLOT_FILE.unlink()
    return True


@traced_activity("coding.brief")
async def draft_coding_brief(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Aura's brief for Claude Code, or ``error`` when two replies in a row could not be read."""
    from app.activities.openclaw_activities import send_to_openclaw
    from app.config import get_coding_config

    repositories = get_coding_config()["repositories"]
    message = prompts.brief_prompt(payload["intent"], repositories, answers=payload.get("answers") or [],
                                   memory=payload.get("memory") or "")
    error = ""
    for _ in range(2):
        retry = f"\n\nYour last reply could not be used ({error}). Reply with the JSON object only." if error else ""
        response = await send_to_openclaw({**_context(payload), "message": message + retry})
        try:
            return prompts.parse_brief(_reply_text(response), repositories)
        except ValueError as exc:
            error = str(exc)
    return {"error": error}


@traced_activity("coding.prepare_workspace")
async def prepare_coding_workspace(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The job's checkout and review repository, the shared test venv, and the test commands Claude is told."""
    from app.config import get_coding_config

    cfg = get_coding_config()
    job = await asyncio.to_thread(workspace.prepare, payload["task_id"], payload["repo"], payload["title"], cfg)
    entry = cfg["repositories"][job.repo]
    if any("{venv}" in arg for argv in entry["tests"] for arg in argv):
        await asyncio.to_thread(verify.ensure_shared_venv, job.repo, Path(job.source) / "requirements.txt")
    await asyncio.to_thread(verify.seed_test_cache)
    tests = verify.commands_for(job, cfg)[-len(entry["tests"]):]
    return {**asdict(job), "tests": tests}


@traced_activity("coding.claude_round")
async def run_claude_round(payload: Dict[str, Any]) -> Dict[str, Any]:
    """One Claude Code run, tailed until its unit exits; reattaches when retried after a worker restart."""
    from app.config import get_coding_config

    cfg = get_coding_config()
    task_id = payload["task_id"]
    spec = runner.RunSpec(task_id=task_id, number=int(payload["number"]), job_dir=Path(payload["checkout"]),
                          prompt=payload["prompt"], system_prompt=payload.get("system_prompt") or "",
                          report_schema=prompts.REPORT_SCHEMA, resume_session=payload.get("resume_session"))
    run = await asyncio.to_thread(runner.start, spec, cfg)
    details = activity.info().heartbeat_details
    noticed_at, noticed_events = (float(details[1]), int(details[2])) if details else (time.time(), 0)
    state = stream.StreamState()
    offset, last_touch, gone_since = 0, 0.0, None
    try:
        while True:
            lines, offset = runner.read_events(run, offset)
            for line in lines:
                stream.feed(state, line)
            activity.heartbeat(offset, noticed_at, noticed_events)
            status = await asyncio.to_thread(runner.status, run)
            if status["exit"] is not None:
                break
            gone_since = None if status["active"] else (gone_since or time.monotonic())
            if gone_since and time.monotonic() - gone_since >= UNIT_GONE_SEC:
                break
            now = time.time()
            if now - last_touch >= LIVENESS_SEC:
                await _touch(task_id)
                last_touch = now
            if now - noticed_at >= NOTICE_SEC and state.events > noticed_events and state.milestones:
                await _notify(payload, f"Claude Code is still on round {payload['round']}. Latest: "
                              + "; ".join(state.milestones[-3:]))
                noticed_at, noticed_events = now, state.events
            await asyncio.sleep(POLL_SEC)
    except asyncio.CancelledError:
        cancel = activity.cancellation_details()
        if cancel is not None and cancel.cancel_requested:
            await asyncio.to_thread(runner.stop, run)
        raise
    lines, offset = runner.read_events(run, offset)
    for line in lines:
        stream.feed(state, line)
    result = stream.outcome(state, exit_line=runner.exit_line(run) or "gone", stopped=run.stop_file.exists())
    await asyncio.to_thread(runner.record_usage, run, result)
    return {"number": run.number, "kind": result.kind, "session_id": result.session_id, "report": result.report,
            "num_turns": result.num_turns, "cost_usd": result.cost_usd, "error": result.error, "exit": result.exit,
            "resume_at": runner.resume_at(result), "commands": state.commands[-30:],
            "files_edited": state.files_edited[:50], "tool_counts": state.tool_counts}


def _live_units(pattern: str) -> List[str]:
    listed = subprocess.run([runner.SYSTEMCTL, "list-units", "--plain", "--no-legend", "--state=active,activating", pattern],
                            capture_output=True, text=True, timeout=30)
    return [line.split()[0] for line in listed.stdout.splitlines() if line.strip()]


def _stop_units(pattern: str) -> List[str]:
    units = _live_units(pattern)
    for unit in units:
        subprocess.run([runner.SYSTEMCTL, "stop", unit], capture_output=True, timeout=runner.STOP_TIMEOUT_SEC + 30)
    return units


def live_task_units(task_id: str) -> List[str]:
    return [unit for kind in UNIT_KINDS for unit in _live_units(f"aura-{kind}-{task_id}-*")]


def stop_task_units(task_id: str) -> List[str]:
    """Stop every live coding unit of a task, and end Aura's Claude sessions in it.

    A Claude run's stop is recorded first, so it reads as stopped.
    """
    from app.coding.direct import end_task_sessions

    stopped = end_task_sessions(task_id, "stopped")
    root = RUNS_DIR / task_id
    for path in sorted(root.iterdir()) if root.is_dir() else []:
        run = runner.Run(task_id, int(path.name), RUNS_DIR) if path.is_dir() and path.name.isdigit() else None
        if run and runner.unit_active(run.unit):
            runner.stop(run)
            stopped.append(run.unit)
    for kind in UNIT_KINDS:
        stopped += _stop_units(f"aura-{kind}-{task_id}-*")
    return stopped


@traced_activity("coding.stop_units")
async def stop_coding_units(payload: Dict[str, Any]) -> List[str]:
    return await asyncio.to_thread(stop_task_units, payload["task_id"])


def _save_diff(task_id: str, number: int, diff: str) -> None:
    (RUNS_DIR / task_id / f"diff-{number}.patch").write_text(diff)


def _load_diff(task_id: str, number: int) -> str:
    try:
        return (RUNS_DIR / task_id / f"diff-{number}.patch").read_text()
    except FileNotFoundError:
        return ""


@traced_activity("coding.verify_round")
async def verify_coding_round(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Collect the round's work into the review repository and run the repo's tests as aura-coder.

    ``error`` reports work RMP could not collect (Claude's next round is told why); the diff is kept on disk.
    """
    from app.config import get_coding_config

    cfg = get_coding_config()
    task_id, number = payload["task_id"], int(payload["number"])
    job = _job(payload)
    units = (f"aura-collect-{task_id}-{number}", f"aura-verify-{task_id}-{number}-*")
    for pattern in units:
        await asyncio.to_thread(_stop_units, pattern)

    def work() -> Dict[str, Any]:
        try:
            collected = workspace.collect(job, payload.get("message") or "", cfg, number=number)
        except RuntimeError as exc:
            return {"error": str(exc), "collected": None, "tests": None}
        _save_diff(task_id, number, collected.pop("diff"))
        return {"error": None, "collected": collected, "tests": verify.run_tests(job, cfg, attempt=number)}

    try:
        return await _heartbeating(task_id, asyncio.to_thread(work))
    except asyncio.CancelledError:
        cancel = activity.cancellation_details()
        if cancel is not None and cancel.cancel_requested:
            for pattern in units:
                await asyncio.to_thread(_stop_units, pattern)
        raise


async def _heartbeating(task_id: str, work) -> Any:
    """Await blocking work in a thread while heartbeating and keeping the task live."""
    worker = asyncio.ensure_future(work)
    last_touch = 0.0
    while not worker.done():
        activity.heartbeat()
        if time.time() - last_touch >= LIVENESS_SEC:
            await _touch(task_id)
            last_touch = time.time()
        await asyncio.wait({worker}, timeout=POLL_SEC)
    return worker.result()


def _job(payload: Dict[str, Any]) -> workspace.Job:
    return workspace.Job(**{k: v for k, v in payload["job"].items() if k in workspace.Job.__dataclass_fields__})


@traced_activity("coding.deploy")
async def deploy_coding_change(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Ship an approved head: a pull request, or for Aura's own code the exact-commit suite and the hand-off."""
    from app.config import get_coding_config

    cfg = get_coding_config()
    job, head = _job(payload), payload["head"]
    entry = cfg["repositories"][job.repo]
    if payload["target"] != "self":
        body = prompts.pr_body(payload["summary"], (payload.get("report") or {}).get("evidence", {}).get("tests"))
        result = await asyncio.to_thread(deploy.open_pull_request, job, head, entry, payload["title"], body)
        await _record_deploy(job.task_id, result)
        return result
    ready = await asyncio.to_thread(deploy.fetch_approved, job, head)
    if ready["status"] != "ready":
        return ready
    suite = await _heartbeating(job.task_id, asyncio.to_thread(deploy.exact_commit_suite, job, head, cfg))
    if not suite["ok"]:
        return {"status": "failed", "tests": suite,
                "summary": f"RMP's full suite failed on the approved commit {head[:12]} ({prompts.tests_line(suite)}), "
                           "so nothing was deployed; main is unchanged."}
    spec = {"task_id": job.task_id, "old": ready["old"], "head": head, "context": _context(payload), "report": payload["report"],
            "suite": {"ok": True, "summary": prompts.tests_line(suite)}, "branch": job.branch, "title": payload["title"],
            "body": prompts.pr_body(payload["summary"], (payload.get("report") or {}).get("evidence", {}).get("tests"))}
    unit = await asyncio.to_thread(deploy.hand_off, spec)
    return {"status": "handed_off", "unit": unit, "summary": f"RMP's full suite passed on {head[:12]}; {unit} deploys it."}


async def _record_deploy(task_id: str, result: Dict[str, Any]) -> None:
    """The same record a self-deploy's unit writes, for the deploy invariants."""
    from app.db.database import AsyncSessionLocal
    from app.db.models import Event

    async with AsyncSessionLocal() as db:
        db.add(Event(correlation_id=task_id, entity_type="task", entity_id=task_id, event_type="coding.deploy",
                     event_payload=result))
        await db.commit()


@traced_activity("coding.refresh_base")
async def refresh_coding_base(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The job with today's main as its base, for a rebase round."""
    job = await asyncio.to_thread(deploy.refresh_base, _job(payload))
    return {**asdict(job), "tests": payload["job"].get("tests") or [],
            "bundle": str(deploy.BUNDLES_DIR / job.task_id / "main.bundle")}


@traced_activity("coding.review_round")
async def review_coding_round(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Aura's verdict on a round and her message to Kirill, in a fresh session per round.

    ``number`` is the run whose diff is reviewed, ``round`` the round Kirill sees.
    """
    from app.activities.openclaw_activities import send_to_openclaw

    round_no = int(payload["round"])
    diff = _load_diff(payload["task_id"], int(payload["number"]))
    message = prompts.review_prompt(payload["brief"], round_no, payload["claude"], payload["evidence"], diff)
    response = await send_to_openclaw({**_context(payload), "message": message, "session_suffix": f"__r{round_no}"})
    return prompts.parse_review(_reply_text(response))


CODING_ACTIVITIES = [
    coding_settings,
    acquire_coding_slot,
    release_coding_slot,
    draft_coding_brief,
    prepare_coding_workspace,
    run_claude_round,
    stop_coding_units,
    verify_coding_round,
    review_coding_round,
    deploy_coding_change,
    refresh_coding_base,
]
