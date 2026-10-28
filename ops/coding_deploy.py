#!/usr/bin/env python3
"""The detached deploy of Aura's code (unit ``aura-deploy-<task>``, as root).

Started by the launcher (``app.coding.deploy.next_deploy``) with the spec of a queued run in the task's runs
directory: a reviewed job's approved commit, queued by the coding workflow's deploy activity, or a commit of
GitHub's main that CI passed, queued by RMP's watcher (``aura-deploy-main``). It runs
``app.coding.deploy.self_deploy``, which takes the code-reload lock within ``LOCK_WAIT_SEC`` once its slow work is
done, and records the result; when the lock stays held or Aura is busy at the final check, it records nothing and
the run stays queued. A run Aura still keeps busy two hours after it was queued is launched with ``--postpone`` and
records ``postponed`` under the lock, taken at once or not at all. Each launch first prints which launch of the run
it is; the run's launches share one log, ``deploy.log``. After a result, a reviewed job gets a fresh run of its
workflow, which sends Aura's judged reply on the new code; a deploy of GitHub's main sends Kirill a note with its
pull requests' links when it did not go live (rolled back, blocked, failed, postponed) or when it changed files
under ``ops/systemd/``, which it never installs.
"""
import asyncio
import fcntl
import json
import signal
import sys
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


def postponed(run: dict) -> dict:
    """The result of a run Aura kept busy for two hours; a reviewed run that landed is on GitHub's main already."""
    if run.get("phase") == "landed":
        return {"status": "postponed", "summary": "Aura was still busy 2 hours after this deploy was queued, so nothing "
                "was deployed here; GitHub's main has the change, and it goes live with GitHub's main once Aura is idle."}
    return {"status": "postponed", "summary": "Aura was still busy 2 hours after this deploy was queued, so nothing was "
            "deployed; main is unchanged."}


def record(spec_path: str, spec: dict, result: dict) -> int:
    """Record the run's result, then remove its state, then ``finish``; the unit's exit status."""
    result = {**result, "suite": spec.get("suite"), "source": spec.get("source") or "reviewed",
              "head": result.get("head") or spec.get("head"), "ci": spec.get("ci")}
    log(f"result: {json.dumps(result)}")
    Path(spec_path).with_name("result.json").write_text(json.dumps(result, indent=2) + "\n")
    deploy.state_file(spec["task_id"]).unlink(missing_ok=True)
    asyncio.run(finish(spec, result))
    return 0 if result["status"] == "deployed" else 1


def interrupt(signum, frame) -> None:
    """``SIGTERM`` (``systemctl stop``, the unit's time cap, a shutdown) as ``deploy.Interrupted``: the switch window
    starts the units it stopped, and ``main`` keeps the run."""
    raise deploy.Interrupted("SIGTERM")


def main(spec_path: str, postpone: bool = False) -> int:
    """Deploy the queued run of ``spec_path``, with ``SIGTERM`` raising ``deploy.Interrupted`` meanwhile."""
    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        return run_launch(spec_path, postpone)
    finally:
        signal.signal(signal.SIGTERM, previous)


def run_launch(spec_path: str, postpone: bool) -> int:
    """One launch of the run of ``spec_path``. Waiting (the lock stayed held, or Aura is busy) records nothing, keeps
    the run's state and exits 0; a result is recorded, then the state is removed, then ``finish`` runs. A postponed
    run records its result under the code-reload lock, taken at once or not at all. An error or ``SIGTERM`` before
    ``applying`` is an attempt (``deploy.record_attempt``): the run is retried, and the third ends it ``failed``. In a
    phase from ``applying`` on it keeps the state, for the next idle pass to resume. Both exit 1."""
    spec = json.loads(Path(spec_path).read_text())
    run = deploy._read_json(deploy.state_file(spec["task_id"])) or {}
    log(f"launch {run.get('launches', 0)} of the deploy of {spec['head'][:12]} (phase {run.get('phase')}"
        f"{', postpone' if postpone else ''})")
    if postpone:
        with open(deploy.CODE_RELOAD_LOCK, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log(f"waiting: {deploy.CODE_RELOAD_LOCK} is held; the run stays queued")
                return 0
            return record(spec_path, spec, postponed(run))
    what = "GitHub's main" if spec.get("source") == "github" else f"{spec['head'][:12]} over {spec['old'][:12]}"
    log(f"deploying {what}")
    try:
        result = deploy.self_deploy(spec, deploy.Host(), log=log)
    except (Exception, deploy.Interrupted) as exc:
        phase = (deploy._read_json(deploy.state_file(spec["task_id"])) or {}).get("phase")
        if phase in deploy.CHANGING:
            log(f"interrupted in phase {phase}; the next idle pass resumes it" if isinstance(exc, deploy.Interrupted)
                else f"stopped in phase {phase} with an error ({str(exc)[:300]}); the next idle pass resumes it")
            return 1
        error = str(exc) or type(exc).__name__
        result = deploy.record_attempt(spec["task_id"], error)
        if result is None:
            log(f"stopped before the change ({error[:300]}); the launcher's next pass tries again")
            return 1
    if result is None:
        log("the run stays queued; the launcher's next pass tries again")
        return 0
    return record(spec_path, spec, result)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], postpone="--postpone" in sys.argv[2:]))
