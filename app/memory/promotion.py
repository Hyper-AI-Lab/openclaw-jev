"""Memory promotion after a delivered reply: queue the IA's facts; keep reusable procedures.

The Internal Agent extracts facts from the task's turns, consolidates them with what is already
known and has Jev review them (``app/deep_memory/facts.py``). Procedural memory is a procedure
summary, written only for multi-step tasks that used tools. Canary, system and heartbeat work
never becomes shared memory.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("rmp.memory.promotion")

MIN_PROMOTION_CONFIDENCE = 70


def build_procedure_summary(
    goal: str,
    steps: List[Dict[str, Any]],
    trace: List[Dict[str, Any]],
    outcome: str,
) -> Optional[str]:
    """What was done, reusable on similar work. None unless several steps ran and tools were used."""
    step_names = [str(s.get("name")) for s in steps or [] if isinstance(s, dict) and s.get("name")]
    tools = list(dict.fromkeys(t["tool"] for t in trace if t.get("tool")))
    if not tools or len(step_names) <= 1:
        return None
    failed = sum(1 for t in trace if t.get("ok") is False)
    lines = [f"Task: {' '.join((goal or '').split())[:300]}"]
    if step_names:
        lines.append("Steps: " + "; ".join(step_names))
    if tools:
        suffix = f" ({failed} call(s) failed)" if failed else ""
        lines.append("Tools used: " + ", ".join(tools) + suffix)
    lines.append(f"Result: {' '.join((outcome or '').split())[:300]}")
    return "\n".join(lines)


def validate_fact(fact: Dict[str, Any]) -> Tuple[bool, str]:
    """Stage C: confidence and provenance quality gate."""
    conf = int(fact.get("confidence", 0))
    content = (fact.get("content") or "").strip()
    if conf < MIN_PROMOTION_CONFIDENCE:
        return False, "low_confidence"
    if len(content) < 15:
        return False, "too_short"
    if "[REDACTED" in content:
        return False, "redacted_content"
    return True, "ok"


async def promote_completion_memory(
    *,
    process_run_id: str,
    process_type: str,
    task_id: str,
    episodic_content: str,
) -> Dict[str, Any]:
    """After the reply reached Kirill: queue fact extraction, and record the procedure if one ran."""
    from app.memory.router import MemoryRouter

    from app.db.database import AsyncSessionLocal
    from app.db.models import ProcessRun, Task
    from app.deep_memory.ingest import enqueue_facts
    from app.notification_policy import is_internal_task
    from app.openclaw_sessions import task_action_trace

    stats = {"facts_queued": 0, "promoted_procedural": 0, "rejected": 0}
    task = run = None
    try:
        async with AsyncSessionLocal() as db:
            task = await db.get(Task, task_id) if task_id else None
            run = await db.get(ProcessRun, process_run_id) if process_run_id else None
    except Exception as exc:
        logger.warning("Promotion lookup failed for task %s: %s", task_id, exc)
    goal = (task.goal if task else "") or ""
    if is_internal_task(goal, (task.task_type if task else "") or process_type or "", []):
        return {**stats, "skipped": "internal_task"}

    if task is not None:
        try:
            await enqueue_facts(task_id)
            stats["facts_queued"] = 1
        except Exception as exc:
            logger.warning("Fact extraction enqueue failed for task %s: %s", task_id, exc)

    steps = (run.plan_json or {}).get("steps") if run and isinstance(run.plan_json, dict) else []
    procedure = build_procedure_summary(goal, steps, task_action_trace(task_id), episodic_content)
    if process_type and procedure:
        try:
            await MemoryRouter.write(
                scope_type="procedural",
                scope_id=process_type,
                memory_type="procedural",
                content=procedure,
                provenance={
                    "task_id": task_id,
                    "promoted_from": "completion_pipeline",
                    "process_type": process_type,
                },
                confidence=85,
            )
            stats["promoted_procedural"] += 1
        except ValueError:
            stats["rejected"] += 1

    return stats
