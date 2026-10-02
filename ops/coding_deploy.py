#!/usr/bin/env python3
"""The detached deploy of Aura's code (unit ``aura-deploy-<task>``, as root).

Started with a spec in the task's runs directory: by the coding workflow's deploy activity for a reviewed
job's approved commit, or by ``deploy_pr`` for GitHub's main once Aura merged a pull request. It holds the
code-reload lock for the whole deploy, runs ``app.coding.deploy.self_deploy`` and records the result. Then
a reviewed job gets a fresh run of its workflow, which sends Aura's judged reply on the new code; a deploy
of Aura's own pull requests sends Kirill a note with their links.
"""
import asyncio
import fcntl
import json
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


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {message}", flush=True)


async def finish(spec: dict, result: dict) -> None:
    """Record the result on the task, then start the run that replies, or tell Kirill about Aura's own deploy."""
    task_id = spec["task_id"]
    try:
        async with AsyncSessionLocal() as db:
            db.add(Event(correlation_id=task_id, entity_type="task", entity_id=task_id, event_type="coding.deploy",
                         event_payload=result))
            task = await db.get(Task, task_id)
            if task is not None:
                task.supplementary_context = {**(task.supplementary_context or {}), "coding_deploy": result}
            await db.commit()
    finally:
        await engine.dispose()
    if spec.get("source") == "github":
        note = deploy.deploy_note(result, get_coding_config()["repositories"]["rmp"]["remote"])
        await notify_ops_slack(note, incident_id=f"deploy:{task_id}:{(result.get('head') or '')[:12]}")
        log("told Kirill")
        return
    client = await connect_temporal()
    await client.start_workflow("CodingTaskWorkflow", {**spec["context"], "report": {**spec["report"], "shipped": result}},
                                id=f"workflow-{task_id}", task_queue=TASK_QUEUE)
    log(f"started the reply run workflow-{task_id}")


def main(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text())
    with open(deploy.CODE_RELOAD_LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        what = "GitHub's main" if spec.get("source") == "github" else f"{spec['head'][:12]} over {spec['old'][:12]}"
        log(f"holding {deploy.CODE_RELOAD_LOCK}; deploying {what}")
        try:
            result = deploy.self_deploy(spec, deploy.Host(), log=log)
        except Exception as exc:
            main_now = deploy._git(deploy.LIVE_REPO, "rev-parse", "refs/heads/main").strip()
            result = {"status": "failed", "error": str(exc)[:500],
                      "summary": f"The deploy stopped with an error ({str(exc)[:300]}); main is at {main_now[:12]}, "
                                 "please check it."}
    result = {**result, "suite": spec.get("suite"), "source": spec.get("source") or "reviewed"}
    log(f"result: {json.dumps(result)}")
    asyncio.run(finish(spec, result))
    return 0 if result["status"] == "deployed" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
