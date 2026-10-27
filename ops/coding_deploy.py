#!/usr/bin/env python3
"""The detached deploy of Aura's code (unit ``aura-deploy-<task>``, as root).

Started by the launcher (``app.coding.deploy.next_deploy``) with the spec of a queued run in the task's runs
directory: a reviewed job's approved commit, queued by the coding workflow's deploy activity, or a commit of
GitHub's main that CI passed, queued by RMP's watcher (``aura-deploy-main``). It takes the code-reload lock
within ``LOCK_WAIT_SEC``, holds it for the whole deploy, runs ``app.coding.deploy.self_deploy`` and records the
result; when it cannot take the lock or Aura is busy, it records nothing and the run stays queued. A run queued
for two hours is launched with ``--postpone`` and records ``postponed`` without the lock. Each launch first prints
which launch of the run it is; the run's launches share one log, ``deploy.log``. After a result, a
reviewed job gets a fresh run of its workflow, which sends Aura's judged reply on the new code; a deploy of
GitHub's main sends Kirill a note with its pull requests' links when it did not go live (rolled back, blocked,
failed, postponed) or when it changed files under ``ops/systemd/``, which it never installs.
"""
import asyncio
import fcntl
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Everything used below is imported now: main moves under this process, and a later import would load the new code.
import app.coding.deploy_checks  # noqa: E402,F401
import app.production.canary_sentinel  # noqa: E402,F401
from app.coding import deploy  # noqa: E402
from app.config import get_coding_config  # noqa: E402
from app.db.database import AsyncSessionLocal, engine  # noqa: E402
from app.db.models import Event, Task  # noqa: E402
from app.production.ops_notify import notify_ops_slack  # noqa: E402
from app.temporal_control import connect_temporal  # noqa: E402

TASK_QUEUE = "openclaw-tasks"
# A deploy of GitHub's main that is live needs no note, unless it changed unit files; the Event records it.
QUIET = ("deployed", "unchanged")
# How often the unit tries the code-reload lock while it waits for it.
LOCK_POLL_SEC = 5


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {message}", flush=True)


async def finish(spec: dict, result: dict) -> None:
    """Record the result on the task, then start the run that replies, or tell Kirill when Aura's own deploy is not live
    or changed unit files."""
    task_id = spec["task_id"]
    try:
        async with AsyncSessionLocal() as db:
            db.add(Event(correlation_id=task_id, entity_type="deploy" if spec.get("source") == "github" else "task",
                         entity_id=task_id, event_type="coding.deploy", event_payload=result))
            task = await db.get(Task, task_id)
            if task is not None:
                task.supplementary_context = {**(task.supplementary_context or {}), "coding_deploy": result}
            await db.commit()
    finally:
        await engine.dispose()
    if spec.get("source") == "github":
        if result["status"] not in QUIET or result.get("units"):
            note = deploy.deploy_note(result, get_coding_config()["repositories"]["rmp"]["remote"])
            await notify_ops_slack(note, incident_id=f"deploy:{task_id}:{(result.get('head') or '')[:12]}")
            log("told Kirill")
        return
    client = await connect_temporal()
    await client.start_workflow("CodingTaskWorkflow", {**spec["context"], "report": {**spec["report"], "shipped": result}},
                                id=f"workflow-{task_id}", task_queue=TASK_QUEUE)
    log(f"started the reply run workflow-{task_id}")


def take_lock(lock) -> bool:
    """Take the code-reload lock within ``deploy.LOCK_WAIT_SEC``, trying every ``LOCK_POLL_SEC``; whether it was taken."""
    deadline = time.monotonic() + deploy.LOCK_WAIT_SEC
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(LOCK_POLL_SEC, left))


def postponed(run: dict) -> dict:
    """The result of a run that stayed queued for two hours; a reviewed run that landed is on GitHub's main already."""
    if run.get("phase") == "landed":
        return {"status": "postponed", "summary": "Aura stayed busy for 2 hours, so nothing was deployed; GitHub's main "
                "has the change, and it goes live with GitHub's main once Aura is idle."}
    return {"status": "postponed", "summary": "Aura stayed busy for 2 hours, so nothing was deployed; main is unchanged."}


def main(spec_path: str, postpone: bool = False) -> int:
    """Deploy the queued run of ``spec_path``. Waiting (the lock stayed held, or Aura is busy) records nothing, keeps
    the run's state and exits 0; a result is recorded, then the state is removed, then ``finish`` runs."""
    spec = json.loads(Path(spec_path).read_text())
    state = deploy.state_file(spec["task_id"])
    run = deploy._read_json(state) or {}
    log(f"launch {run.get('launches', 0)} of the deploy of {spec['head'][:12]} (phase {run.get('phase')}"
        f"{', postpone' if postpone else ''})")
    if postpone:
        result = postponed(run)
    else:
        with open(deploy.CODE_RELOAD_LOCK, "w") as lock:
            if not take_lock(lock):
                log(f"waiting: {deploy.CODE_RELOAD_LOCK} stayed held for {deploy.LOCK_WAIT_SEC} s; the run stays queued")
                return 0
            what = "GitHub's main" if spec.get("source") == "github" else f"{spec['head'][:12]} over {spec['old'][:12]}"
            log(f"holding {deploy.CODE_RELOAD_LOCK}; deploying {what}")
            try:
                result = deploy.self_deploy(spec, deploy.Host(), log=log)
            except Exception as exc:
                main_now = deploy._git(deploy.LIVE_REPO, "rev-parse", "refs/heads/main").strip()
                result = {"status": "failed", "error": str(exc)[:500],
                          "summary": f"The deploy stopped with an error ({str(exc)[:300]}); main is at {main_now[:12]}, "
                                     "please check it."}
            if result is None:
                log("the run stays queued; the launcher's next pass tries again")
                return 0
    result = {**result, "suite": spec.get("suite"), "source": spec.get("source") or "reviewed",
              "head": result.get("head") or spec.get("head"), "ci": spec.get("ci")}
    log(f"result: {json.dumps(result)}")
    Path(spec_path).with_name("result.json").write_text(json.dumps(result, indent=2) + "\n")
    state.unlink(missing_ok=True)
    asyncio.run(finish(spec, result))
    return 0 if result["status"] == "deployed" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], postpone="--postpone" in sys.argv[2:]))
