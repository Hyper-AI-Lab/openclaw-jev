#!/usr/bin/env python3
"""The detached deploy of Aura's code (unit ``aura-deploy-<task>``, as root).

Started by the launcher (``app.coding.deploy.next_deploy``) with the spec of a queued run in the task's runs
directory: a reviewed job's approved commit, queued by the coding workflow's deploy activity, or a commit of
GitHub's main that CI passed, queued by RMP's watcher (``aura-deploy-main``). It runs
``app.coding.deploy.self_deploy``, which takes the code-reload lock within ``LOCK_WAIT_SEC`` once its slow work is
done, and records the result under that lock; when the lock stays held or Aura is busy at the final check, it records
nothing and the run stays queued. A result reached before the lock (unchanged, blocked, failed) is recorded under the
lock taken at once, or the run waits. A run Aura still keeps busy two hours after it was queued is launched with
``--postpone`` and records ``postponed`` under the lock, taken at once or not at all. Each launch first prints which
launch of the run it is, then checks in; the run's launches share one log, ``deploy.log``. An error or ``SIGTERM``
before the change is retried by the next launch, at most three times; one from ``applying`` on leaves the run to be
resumed from its phase. After a result, a reviewed job gets a fresh run of its workflow, which sends Aura's judged
reply on the new code; a deploy of GitHub's main sends Kirill a note with its pull requests' links when it did not go
live (rolled back, blocked, failed, postponed), when it changed files under ``ops/systemd/``, which it never installs,
or when readiness checks it added or tightened fail on this host, which do not count against it.
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
# A deploy of GitHub's main that is live needs no note, unless it changed unit files or new or tightened readiness
# checks fail; the Event records it.
QUIET = ("deployed", "unchanged")


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {message}", flush=True)


async def finish(spec: dict, result: dict) -> None:
    """Tell Kirill first, on its own and retried (``deploy.send_note``), when Aura's own deploy is not live, changed
    unit files or found new or tightened readiness checks failing; then record the result on the task, with whether the
    note was delivered; then, for a reviewed job, start the run that replies. Each step runs whatever the one before
    did, and a failure is logged."""
    task_id, github = spec["task_id"], spec.get("source") == "github"
    recorded = result
    if github and (result["status"] not in QUIET or result.get("units") or result.get("new_failing")):
        noted = False
        try:
            note = deploy.deploy_note(result, get_coding_config()["repositories"]["rmp"]["remote"])
        except Exception as exc:
            log(f"writing the note to Kirill failed ({str(exc)[:300]})")
        else:
            noted = await deploy.send_note(note, f"deploy:{task_id}:{(result.get('head') or '')[:12]}",
                                           notify_ops_slack, log)
        recorded = {**result, "note_delivered": noted}
    try:
        async with AsyncSessionLocal() as db:
            db.add(Event(correlation_id=task_id, entity_type="deploy" if github else "task", entity_id=task_id,
                         event_type="coding.deploy", event_payload=recorded))
            task = await db.get(Task, task_id)
            if task is not None:
                task.supplementary_context = {**(task.supplementary_context or {}), "coding_deploy": recorded}
            await db.commit()
    except Exception as exc:
        log(f"recording the result on the task failed ({str(exc)[:300]})")
    finally:
        await engine.dispose()
    if github:
        return
    try:
        client = await connect_temporal()
        await client.start_workflow("CodingTaskWorkflow",
                                    {**spec["context"], "report": {**spec["report"], "shipped": result}},
                                    id=f"workflow-{task_id}", task_queue=TASK_QUEUE)
        log(f"started the reply run workflow-{task_id}")
    except Exception as exc:
        log(f"starting the reply run workflow-{task_id} failed ({str(exc)[:300]})")


def postponed(run: dict) -> dict:
    """The result of a run Aura kept busy for two hours; a reviewed run that landed is on GitHub's main already."""
    if run.get("phase") == "landed":
        return {"status": "postponed", "summary": "Aura was still busy 2 hours after this deploy was queued, so "
                "nothing was deployed here; GitHub's main has the change, and it goes live with GitHub's main once "
                "Aura is idle."}
    return {"status": "postponed", "summary": "Aura was still busy 2 hours after this deploy was queued, so nothing "
            "was deployed; main is unchanged."}


def record(spec_path: str, spec: dict, result: dict) -> int:
    """Record the run's result (atomically, on disk before the state goes), then remove its state, then ``finish``;
    the unit's exit status."""
    result = {**result, "suite": spec.get("suite"), "source": spec.get("source") or "reviewed",
              "head": result.get("head") or spec.get("head"), "ci": spec.get("ci")}
    log(f"result: {json.dumps(result)}")
    deploy.write_json(Path(spec_path).with_name("result.json"), result)
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
    """One launch of the run of ``spec_path``. It first checks in (``deploy.mark_started``) and marks its end
    (``deploy.mark_ended``) once it returns; a run without a state does nothing. Waiting (the lock stayed held,
    or Aura is busy) records nothing, keeps the run's state and exits 0; a result is recorded, then the state is
    removed, then ``finish`` runs. A postponed run records its result under the code-reload lock, taken at once or not
    at all. An error or ``SIGTERM`` before ``applying`` is an attempt (``deploy.record_attempt``): the run is retried,
    and the third ends it ``failed``. In a phase from ``applying`` on it keeps the state, for the next idle pass to
    resume, and is counted as an escape. Both exit 1."""
    spec = json.loads(Path(spec_path).read_text())
    task = spec["task_id"]
    run = deploy._read_json(deploy.state_file(task)) or {}
    log(f"launch {run.get('launches', 0)} of the deploy of {spec['head'][:12]} (phase {run.get('phase')}"
        f"{', postpone' if postpone else ''})")
    if not deploy.mark_started(task):
        log(f"the run of {task} has no state; nothing to do")
        return 0
    code = checked_in(spec_path, spec, run, postpone)
    # Only a launch that returned ends here. An exception that escapes it after the check-in (an I/O error in
    # record, say) counts like a kill without a word: the next launch folds it in as an attempt or an escape.
    deploy.mark_ended(task)
    return code


def checked_in(spec_path: str, spec: dict, run: dict, postpone: bool) -> int:
    """The launch's work once it checked in, with ``run`` its state as the launch found it.

    Every result is recorded, and ``finish`` runs, under the code-reload lock, so none is written while a restore or a
    code reload holds it: a result reached under the lock without letting go of it (``Host.keeping``), any other under
    a hold taken at once (``LOCK_NB``). When that lock is held, the launch waits and the run keeps its state; a last
    attempt is then not counted, and the next launch tries again."""
    task, state = spec["task_id"], deploy.state_file(spec["task_id"])
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
    host = deploy.Host()
    with host.keeping(deploy.CODE_RELOAD_LOCK):
        try:
            result = deploy.self_deploy(spec, host, log=log)
        except (Exception, deploy.Interrupted) as exc:
            now = deploy._read_json(state) or {}
            if now.get("phase") in deploy.CHANGING:
                # Counted, so a resume that fails the same way every time ends dead (deploy.end_dead_runs), and marked
                # ended in the same write, so a kill right after cannot count it twice.
                deploy.write_json(state, {**now, "escapes": now.get("escapes", 0) + 1,
                                          "ended": now.get("started", now.get("launches", 0))})
                log(f"interrupted in phase {now['phase']}; the next idle pass resumes it"
                    if isinstance(exc, deploy.Interrupted) else
                    f"stopped in phase {now['phase']} with an error ({str(exc)[:300]}); the next idle pass resumes it")
                return 1
            error = str(exc) or type(exc).__name__
            with host.hold(deploy.CODE_RELOAD_LOCK, 0) as held:
                if not held and now.get("attempt", 0) + 1 >= deploy.MAX_ATTEMPTS:
                    log(f"waiting: {deploy.CODE_RELOAD_LOCK} is held, so the last attempt ({error[:300]}) is not "
                        "counted; the launcher's next pass tries again")
                    return 0
                result = deploy.record_attempt(task, error)
                if result is None:
                    log(f"stopped before the change ({error[:300]}); the launcher's next pass tries again")
                    return 1
                return record(spec_path, spec, result)
        if result is None:
            log("the run stays queued; the launcher's next pass tries again")
            return 0
        with host.hold(deploy.CODE_RELOAD_LOCK, 0) as held:
            if not held:
                log(f"waiting: {deploy.CODE_RELOAD_LOCK} is held, so the result ({result['status']}) is not recorded; "
                    "the run keeps its state")
                return 0
            return record(spec_path, spec, result)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], postpone="--postpone" in sys.argv[2:]))
