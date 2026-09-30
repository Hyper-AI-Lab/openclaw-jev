"""Temporal activities of the deep recall child workflow; each records its step in dm_context_reports."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from temporalio import activity

from app.db.database import AsyncSessionLocal
from app.db.models import DeepContextReport
from app.deep_memory import recall
from app.orchestrator.prompt_policy import USER_TIMEZONE


async def _update(report_id: str, **fields: Any) -> DeepContextReport:
    async with AsyncSessionLocal() as db:
        row = await db.get(DeepContextReport, report_id)
        for key, value in fields.items():
            setattr(row, key, value)
        await db.commit()
        return row


def _latency_ms(row: DeepContextReport) -> int:
    return int((datetime.utcnow() - row.created_at).total_seconds() * 1000) if row.created_at else 0


@activity.defn
async def start_recall_report(payload: Dict[str, Any]) -> str:
    """The report row, under the id the task chose when it has one (a retry finds it written)."""
    async with AsyncSessionLocal() as db:
        if payload.get("report_id") and await db.get(DeepContextReport, payload["report_id"]):
            return payload["report_id"]
        row = DeepContextReport(task_id=payload["task_id"], trigger=payload.get("trigger") or "task_start",
                                status="running")
        if payload.get("report_id"):
            row.id = payload["report_id"]
        db.add(row)
        await db.commit()
        return row.id


@activity.defn
async def plan_recall_step(payload: Dict[str, Any]) -> Dict[str, Any]:
    from app.task_registry.messages import recent_session_dialogue_block

    dialogue = await recent_session_dialogue_block(payload.get("session_key") or "", exclude_task_id=payload["task_id"])
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(USER_TIMEZONE)).strftime("%Y-%m-%d (%A)")
    plan = await recall.plan_recall(payload.get("query") or "", dialogue, today)
    await _update(payload["report_id"], queries=plan.model_dump())
    return {"plan": plan.model_dump(), "dialogue": dialogue}


@activity.defn
async def retrieve_recall_step(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    evidence = await recall.retrieve(recall.RecallPlan(**payload["plan"]), exclude_task_id=payload["task_id"])
    await _update(payload["report_id"], candidate_ids=[e.ref for e in evidence])
    return recall.evidence_dicts(evidence)


@activity.defn
async def read_recall_step(payload: Dict[str, Any]) -> Dict[str, Any]:
    report, usage = await recall.read(
        payload.get("query") or "", payload.get("dialogue") or "", recall.evidence_from_dicts(payload["evidence"])
    )
    status = "ready" if report["relevant"] else "empty"
    async with AsyncSessionLocal() as db:
        row = await db.get(DeepContextReport, payload["report_id"])
        row.report, row.brief, row.usage, row.status = report, report["brief"], usage, status
        row.completed_at = datetime.utcnow()
        row.latency_ms = _latency_ms(row)
        await db.commit()
        latency = row.latency_ms
    return {"status": status, "report_id": payload["report_id"], "report": report, "latency_ms": latency}


@activity.defn
async def close_recall_report(payload: Dict[str, Any]) -> None:
    async with AsyncSessionLocal() as db:
        row = await db.get(DeepContextReport, payload["report_id"])
        row.status = payload["status"]
        row.report = {**(row.report or {}), "reason": payload.get("reason") or ""}
        row.completed_at = datetime.utcnow()
        row.latency_ms = _latency_ms(row)
        await db.commit()


@activity.defn
async def judge_recall_novelty(payload: Dict[str, Any]) -> Dict[str, Any]:
    verdict, usage = await recall.judge_novelty(
        payload.get("query") or "", payload.get("reply") or "", payload.get("report") or {}
    )
    await _update(payload["report_id"], novelty={**verdict, "usage": usage})
    return verdict


@activity.defn
async def settle_recall_report(payload: Dict[str, Any]) -> None:
    """How the task used its recall; a report still running when the task let it go is closed."""
    async with AsyncSessionLocal() as db:
        row = await db.get(DeepContextReport, payload["report_id"])
        if row is None:
            return
        row.consumed_by = payload.get("consumed_by") or row.consumed_by
        if payload.get("status") and row.status == "running":
            row.status = payload["status"]
            row.report = {**(row.report or {}), "reason": payload.get("reason") or ""}
            row.completed_at = datetime.utcnow()
            row.latency_ms = _latency_ms(row)
        await db.commit()


RECALL_ACTIVITIES = [
    start_recall_report, plan_recall_step, retrieve_recall_step, read_recall_step, close_recall_report,
    judge_recall_novelty, settle_recall_report,
]
