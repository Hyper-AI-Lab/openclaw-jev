import asyncio
import hashlib
import logging
import os
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from temporalio.client import Client

from app.config import (
    get_api_key,
    get_task_registry_intake_mode,
    is_task_registry_enabled,
    load_settings,
    save_settings,
    is_development_mode,
    should_suspend_interception,
)
from app.db.database import get_db, init_db
from app.db.models import Artifact, Event, MemoryItem, Observation, ProcessRun, Task
from app.metrics import format_prometheus, get_counters, inc as metrics_inc
from app.production.readiness import get_last_backup_info, run_all_checks
from app.memory.router import MemoryRouter
from app.cron.reconciler import cron_reconciler_loop, reconcile_cron_once
from app.reconciler import reconciler_loop
from app.scanners.catalog import list_scanners
from app.scanners.sync import get_scanner_status, scanner_monitor_loop, sync_scanners_once
from app.telemetry import (
    init_telemetry,
    instrument_fastapi,
    telemetry_status,
    trace_span,
    get_temporal_client_kwargs,
)
from app.orchestrator.prompt_policy import resolve_generic_profile
from app.workflows.catalog import CATALOG, catalog_type_for_workflow, list_catalog, resolve_catalog_template

logger = logging.getLogger("rmp.api")
_reconciler_stop: Optional[asyncio.Event] = None
_reconciler_task: Optional[asyncio.Task] = None
_scanner_stop: Optional[asyncio.Event] = None
_scanner_task: Optional[asyncio.Task] = None
_cron_stop: Optional[asyncio.Event] = None
_cron_task: Optional[asyncio.Task] = None

from app.config import (
    AUTH_PROFILES_PATH,
    OPENCLAW_CONFIG_PATH,
    SETTINGS_PATH,
)

MODEL_CATALOG = {
    "google": ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-3.1-pro-preview"],
    "openai": ["gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini", "gpt-4o", "o3-mini"],
    "anthropic": ["claude-3-5-haiku-latest", "claude-3-7-sonnet-latest"],
    "mistral": ["mistral-large-latest", "mistral-large-2512", "mistral-medium-2505"],
    "nvidia": [
        "minimaxai/minimax-m3",
        "deepseek-ai/deepseek-v4-flash-0731",
        "z-ai/glm-5.2",
        "nvidia/nemotron-3-nano-30b-a3b",
    ],
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _reconciler_stop, _reconciler_task, _scanner_stop, _scanner_task
    global _cron_stop, _cron_task
    await init_db()
    load_settings()
    init_telemetry("rmp-api")
    instrument_fastapi(app)
    try:
        from app.production.runtime_sync import mark_runtime_boot

        mark_runtime_boot("rmp-api")
    except Exception as exc:
        logger.warning("runtime boot stamp failed: %s", exc)
    _reconciler_stop = asyncio.Event()
    _reconciler_task = asyncio.create_task(reconciler_loop(_reconciler_stop))
    _scanner_stop = asyncio.Event()
    _scanner_task = asyncio.create_task(scanner_monitor_loop(_scanner_stop))
    _cron_stop = asyncio.Event()
    _cron_task = asyncio.create_task(cron_reconciler_loop(_cron_stop))
    logger.info("RMP API started with reconciler, scanner monitor, cron reconciler")
    yield
    if _reconciler_stop:
        _reconciler_stop.set()
    if _reconciler_task:
        await _reconciler_task
    if _scanner_stop:
        _scanner_stop.set()
    if _scanner_task:
        await _scanner_task
    if _cron_stop:
        _cron_stop.set()
    if _cron_task:
        await _cron_task


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)


def _verify_api_key(request: Request) -> None:
    api_key = get_api_key()
    if not api_key:
        return
    provided = request.headers.get("X-RMP-API-Key") or request.headers.get(
        "Authorization", ""
    ).removeprefix("Bearer ").strip()
    if provided != api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing RMP API key")


class TaskRequest(BaseModel):
    intent: str
    tags: List[str] = []
    user_id: str = "unknown"
    session_key: str = "agent:main:main"
    raw_text: str = ""
    idempotency_key: Optional[str] = None
    process_type_hint: Optional[str] = None
    task_kind_hint: Optional[str] = None


class SignalRequest(BaseModel):
    message: str = ""
    signal_type: str = "user_input"  # user_input | cancel | approve | retry


class ConfigRequest(BaseModel):
    provider: str
    model: str
    api_key: Optional[str] = None


class SettingsRequest(BaseModel):
    intermediate_updates: Optional[bool] = None
    development_mode: Optional[bool] = None
    suspend_slack_notifications: Optional[bool] = None
    suspend_task_interception: Optional[bool] = None


class MemoryWriteRequest(BaseModel):
    scope_type: str = "process"
    scope_id: str
    memory_type: str = "working"
    content: str
    confidence: int = 100


class MemoryLookupRequest(BaseModel):
    scope_type: str = "process"
    scope_id: str
    memory_type: Optional[str] = None
    limit: int = 20
    query: Optional[str] = None


class MemoryCompactRequest(BaseModel):
    max_age_days: int = 30


class ArtifactRegisterRequest(BaseModel):
    process_run_id: str
    kind: str = "blob"
    content: str
    content_encoding: str = "utf-8"
    filename: Optional[str] = None
    mime_type: Optional[str] = None


async def connect_temporal() -> Client:
    return await Client.connect("localhost:7233", **get_temporal_client_kwargs())


async def _start_task_workflow(
    task_id: str,
    intent: str,
    session_key: str,
    task_type: str,
    correlation_id: Optional[str] = None,
    workflow_name: Optional[str] = None,
    process_type: Optional[str] = None,
    tags: Optional[List[str]] = None,
    initial_memory_block: Optional[str] = None,
    task_kind: Optional[str] = None,
    execution_mode: Optional[str] = None,
) -> None:
    from app.temporal_control import start_task_workflow

    await start_task_workflow(
        task_id,
        intent,
        session_key,
        task_type,
        correlation_id=correlation_id,
        workflow_name=workflow_name,
        process_type=process_type,
        tags=tags,
        initial_memory_block=initial_memory_block,
        task_kind=task_kind,
        execution_mode=execution_mode,
    )


ACTIVE_TASK_STATUSES = frozenset(
    {"created", "running", "pending", "pending_user_input", "blocked", "needs_replan"}
)
TERMINAL_TASK_STATUSES = frozenset(
    {"failed", "completed", "stopped_by_user", "cancelled", "compensated"}
)


def _make_idempotency_key(request: TaskRequest) -> str:
    if request.idempotency_key:
        return request.idempotency_key
    raw = f"{request.session_key}:{request.raw_text or request.intent}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _fresh_idempotency_key(base_key: str) -> str:
    """New key when a terminal prior task should not block a repeat request."""
    return hashlib.sha256(f"{base_key}:v2:{time.time()}".encode()).hexdigest()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    public_paths = {"/", "/health", "/metrics"}
    if request.url.path in public_paths or request.url.path.startswith("/api/dashboard") or request.url.path.startswith("/api/scanners") or request.url.path in ("/api/workflow-catalog", "/memory/vector/status", "/telemetry/status"):
        return await call_next(request)
    if request.url.path == "/settings" and request.method == "GET":
        return await call_next(request)
    client_host = request.client.host if request.client else ""
    if client_host in ("127.0.0.1", "::1") and request.url.path in (
        "/config",
        "/settings",
    ):
        return await call_next(request)
    if client_host in ("127.0.0.1", "::1") and (
        request.url.path.endswith("/cancel")
        or request.url.path.endswith("/retry")
        or request.url.path.endswith("/export")
        or request.url.path == "/tasks/intake/preview"
        or request.url.path.endswith("/spawn_process")
        or request.url.path == "/dev/suspend-all"
        or request.url.path == "/artifacts/register"
        or request.url.path == "/api/scanners/sync"
        or request.url.path.startswith("/artifacts/")
    ):
        return await call_next(request)
    _verify_api_key(request)
    return await call_next(request)


@app.get("/metrics")
async def prometheus_metrics():
    return Response(content=format_prometheus(), media_type="text/plain; version=0.0.4")


@app.get("/health")
async def health():
    vm = await MemoryRouter.vector_status()
    return {
        "status": "ok",
        "service": "rmp",
        "vector_memory": vm,
        "telemetry": telemetry_status(),
    }


@app.get("/telemetry/status")
async def get_telemetry_status():
    return telemetry_status()


@app.post("/tasks/intake/preview")
async def preview_task_intake(
    request: TaskRequest, db: AsyncSession = Depends(get_db)
):
    """Dry-run universal task intake — no task or workflow created."""
    intent = request.intent
    if intent.startswith("[cron:"):
        intent = intent.split("]", 1)[-1].strip() if "]" in intent else intent
    from app.task_registry.intake_runner import run_classify_task_intake
    from app.task_registry.recurrence import derive_recurrence_key

    recurrence_key = derive_recurrence_key(
        request.session_key, request.intent, request.tags
    )
    is_canary = (
        "canary" in (request.tags or [])
        or "RMP CANARY" in intent.upper()
    )
    preview_task_type = "canary" if is_canary else "user"
    result = await run_classify_task_intake(
        {
            "intent": intent,
            "session_key": request.session_key,
            "tags": request.tags or [],
            "process_type_hint": request.process_type_hint,
            "recurrence_key": recurrence_key,
            "task_type": preview_task_type,
        }
    )
    return {"preview": True, "intake": result}


@app.post("/tasks")
async def create_task(
    request: TaskRequest, db: AsyncSession = Depends(get_db)
):
    if should_suspend_interception():
        raise HTTPException(
            status_code=503,
            detail="RMP task creation suspended (development_mode)",
        )

    idem_key = _make_idempotency_key(request)

    existing = await db.execute(
        select(Task).where(Task.idempotency_key == idem_key)
    )
    prior = existing.scalar_one_or_none()
    if prior:
        if prior.status in ACTIVE_TASK_STATUSES:
            return {
                "task_id": prior.id,
                "status": prior.status,
                "deduplicated": True,
            }
        if prior.status in TERMINAL_TASK_STATUSES:
            idem_key = _fresh_idempotency_key(idem_key)
        else:
            return {
                "task_id": prior.id,
                "status": prior.status,
                "deduplicated": True,
            }

    raw_intent = request.intent
    intent = raw_intent
    if intent.startswith("[cron:"):
        intent = intent.split("]", 1)[-1].strip() if "]" in intent else intent

    is_canary = (
        "canary" in (request.tags or [])
        or "intake-smoke" in (request.tags or [])
        or "RMP CANARY" in intent.upper()
    )
    is_cron = (
        (request.raw_text or "").startswith("[cron:")
        or "cron" in (request.tags or [])
    )
    is_heartbeat = "heartbeat" in (request.raw_text or "").lower() and (
        "HEARTBEAT.md" in (request.raw_text or "")
        or "[cron:" in (request.raw_text or "")
    )
    if is_canary:
        task_type = "canary"
    elif is_cron:
        task_type = "cron"
    elif is_heartbeat:
        task_type = "heartbeat"
    else:
        task_type = "user"

    from app.task_registry.recurrence import derive_recurrence_key, derive_task_kind

    recurrence_key = derive_recurrence_key(
        request.session_key, raw_intent, request.tags
    )
    task_kind = derive_task_kind(
        recurrence_key,
        request.tags,
        task_kind_hint=request.task_kind_hint,
    )

    guided_memory = ""
    intake_decision_id = None
    intake_catalog_type = None
    execution_mode = None
    intake_result: dict = {}
    web_capability_block = ""

    if is_task_registry_enabled() and get_task_registry_intake_mode() != "off":
        from app.task_registry.intake_runner import run_classify_task_intake
        from app.task_registry.intake_handlers import handle_intake_outcome

        intake_result = await run_classify_task_intake(
            {
                "intent": intent,
                "session_key": request.session_key,
                "tags": request.tags or [],
                "process_type_hint": request.process_type_hint,
                "recurrence_key": recurrence_key,
                "task_type": task_type,
            }
        )
        intake_catalog_type = intake_result.get("catalog_type")
        execution_mode = intake_result.get("execution_mode")
        wc = intake_result.get("web_capability") or {}
        web_capability_block = (wc.get("web_brief") or "").strip()
        outcome = await handle_intake_outcome(
            intake_result,
            request=request,
            db=db,
            intent=intent,
            session_key=request.session_key,
            tags=request.tags or [],
        )
        if outcome:
            intake_decision_id = outcome.get("intake_decision_id")
            if outcome.get("execution_mode"):
                execution_mode = outcome.get("execution_mode")
            if outcome.get("skipped"):
                await db.commit()
                return outcome
            if outcome.get("intake_action") == "wait_active":
                await db.commit()
                return outcome
            if outcome.get("intake_action") == "attach_active":
                tid = outcome.get("task_id")
                if tid and outcome.get("signal_required"):
                    try:
                        client = await connect_temporal()
                        handle = client.get_workflow_handle(f"workflow-{tid}")
                        await handle.signal("user_input", intent)
                    except Exception as exc:
                        logger.warning("Intake attach signal failed: %s", exc)
                        await db.commit()
                        raise HTTPException(
                            status_code=502,
                            detail={
                                "intake_action": "attach_active",
                                "task_id": tid,
                                "error": str(exc)[:300],
                            },
                        )
                await db.commit()
                return outcome
            if outcome.get("intake_action") == "spawn_process":
                await db.commit()
                return outcome
            guided_memory = outcome.get("_guided_memory_block") or ""

    catalog_type = intake_catalog_type or catalog_type_for_workflow(
        request.process_type_hint, intent, task_type
    )
    if catalog_type:
        task_type = catalog_type

    task_id = str(uuid.uuid4())
    generic_profile = resolve_generic_profile(intent) if not catalog_type else None

    task = Task(
        id=task_id,
        correlation_id=task_id,
        idempotency_key=idem_key,
        requester=request.user_id,
        openclaw_session_key=request.session_key,
        task_type=task_type,
        goal=intent,
        status="created",
        task_kind=task_kind,
        recurrence_key=recurrence_key,
        intake_decision_id=intake_decision_id,
    )
    db.add(task)
    db.add(
        Event(
            correlation_id=task_id,
            entity_type="task",
            entity_id=task_id,
            event_type="task.created",
            event_payload={
                "intent": intent[:300],
                "task_type": task_type,
                "catalog": bool(catalog_type),
                "task_kind": task_kind,
                "recurrence_key": recurrence_key,
            },
        )
    )
    await db.flush()

    from app.task_registry.messages import add_task_message

    msg_source = "cron" if is_cron else "slack" if task_type == "user" else "api"
    msg_id = await add_task_message(
        task_id,
        request.raw_text or intent,
        role="user" if task_type == "user" else task_type,
        source=msg_source,
        db=db,
    )
    if msg_id:
        task.supplementary_context = {
            "latest_message_id": msg_id,
            "source": msg_source,
            "session_key": request.session_key,
        }
    await db.commit()
    metrics_inc("task_created")

    process_run_id = None
    try:
        from app.activities.db_activities import ensure_process_run

        process_run_id = await ensure_process_run(
            {
                "task_id": task_id,
                "process_type": catalog_type or task_type or "generic_task",
            }
        )
    except Exception as exc:
        logger.warning("Eager process run creation skipped: %s", exc)

    initial_memory_block = guided_memory
    if process_run_id:
        try:
            tag_set = {t.lower() for t in (request.tags or [])}
            skip_vector = is_canary or "memory-canary" in tag_set
            prefetched = await MemoryRouter.build_context_block(
                process_run_id=process_run_id,
                task_id=task_id,
                process_type=catalog_type or task_type,
                query=None if skip_vector else intent[:300],
                skip_vector=skip_vector,
            )
            initial_memory_block = "\n\n".join(
                p for p in (guided_memory, web_capability_block, prefetched) if p
            ).strip()
        except Exception as exc:
            logger.warning("Memory prefetch skipped: %s", exc)
    elif web_capability_block:
        initial_memory_block = "\n\n".join(
            p for p in (guided_memory, web_capability_block) if p
        ).strip()

    try:
        with trace_span(
            "task.create",
            {
                "rmp.task_id": task_id,
                "rmp.task_type": task_type,
                "rmp.catalog": str(bool(catalog_type)),
            },
        ):
            await _start_task_workflow(
                task_id,
                request.raw_text or intent,
                request.session_key,
                task_type,
                process_type=catalog_type,
                tags=request.tags or [],
                initial_memory_block=initial_memory_block or None,
                task_kind=task_kind,
                execution_mode=execution_mode,
            )
    except Exception as e:
        task.status = "failed"
        metrics_inc("task_failed")
        await db.commit()
        from app.production.alerting import send_alert

        await send_alert(
            "task.failed",
            f"Workflow start failed for task {task_id[:8]}",
            severity="error",
            context={"task_id": task_id, "error": str(e)[:300]},
        )
        raise HTTPException(status_code=500, detail=f"Workflow start failed: {str(e)}")

    return {
        "task_id": task_id,
        "status": "created",
        "process_run_id": process_run_id,
    }


@app.post("/tasks/{task_id}/spawn_process")
async def spawn_process(task_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    from app.task_registry.spawn import spawn_process_for_task

    proc = await spawn_process_for_task(
        task_id,
        process_type=task.task_type or "generic_task",
        db=db,
    )
    await db.commit()
    return proc


@app.get("/tasks/{task_id}/export")
async def export_task(task_id: str, db: AsyncSession = Depends(get_db)):
    """Postmortem bundle: task, events, observations, artifact metadata."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    corr = task.correlation_id or task_id
    ev_result = await db.execute(
        select(Event)
        .where(
            (Event.entity_id == task_id)
            | (Event.correlation_id == corr)
        )
        .order_by(Event.occurred_at.asc())
    )
    events = [
        {
            "id": e.id,
            "correlation_id": e.correlation_id,
            "entity_type": e.entity_type,
            "entity_id": e.entity_id,
            "event_type": e.event_type,
            "event_payload": e.event_payload,
            "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
        }
        for e in ev_result.scalars().all()
    ]

    pr_result = await db.execute(
        select(ProcessRun).where(ProcessRun.task_id == task_id).order_by(ProcessRun.started_at.asc())
    )
    process_runs = []
    all_observations = []
    all_artifacts = []
    for run in pr_result.scalars().all():
        process_runs.append(
            {
                "id": run.id,
                "process_type": run.process_type,
                "current_state": run.current_state,
                "parent_process_run_id": run.parent_process_run_id,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "ended_at": run.ended_at.isoformat() if run.ended_at else None,
            }
        )
        obs_result = await db.execute(
            select(Observation)
            .where(Observation.process_run_id == run.id)
            .order_by(Observation.observed_at.asc())
        )
        for obs in obs_result.scalars().all():
            all_observations.append(
                {
                    "id": obs.id,
                    "process_run_id": obs.process_run_id,
                    "source": obs.source,
                    "observation_type": obs.observation_type,
                    "payload_ref": obs.payload_ref,
                    "payload_hash": obs.payload_hash,
                    "observed_at": obs.observed_at.isoformat() if obs.observed_at else None,
                    "confidence": obs.confidence,
                }
            )
        art_result = await db.execute(
            select(Artifact)
            .where(Artifact.process_run_id == run.id)
            .order_by(Artifact.created_at.asc())
        )
        for art in art_result.scalars().all():
            all_artifacts.append(
                {
                    "id": art.id,
                    "process_run_id": art.process_run_id,
                    "kind": art.kind,
                    "uri": art.uri,
                    "checksum": art.checksum,
                    "mime_type": art.mime_type,
                    "filename": art.filename,
                    "size_bytes": art.size_bytes,
                    "created_at": art.created_at.isoformat() if art.created_at else None,
                    "download_url": f"/artifacts/{art.id}/download",
                }
            )

    return {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "task": {
            "id": task.id,
            "correlation_id": task.correlation_id,
            "status": task.status,
            "goal": task.goal,
            "task_type": task.task_type,
            "requester": task.requester,
            "openclaw_session_key": task.openclaw_session_key,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        },
        "process_runs": process_runs,
        "events": events,
        "observations": all_observations,
        "artifacts": all_artifacts,
    }


@app.get("/tasks/{task_id}")
async def get_task_status(task_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "task_id": task.id,
        "status": task.status,
        "goal": task.goal,
        "task_type": task.task_type,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, db: AsyncSession = Depends(get_db)):
    """Terminate workflow and mark task stopped (dashboard / dev use)."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        client = await connect_temporal()
        handle = client.get_workflow_handle(f"workflow-{task_id}")
        try:
            await handle.signal("cancel", "Cancelled via API")
        except Exception:
            pass
        await handle.terminate("Cancelled via API")
    except Exception as e:
        logger.warning("Terminate workflow %s: %s", task_id, e)
    task.status = "stopped_by_user"
    task.next_check_at = None
    pr_result = await db.execute(
        select(ProcessRun)
        .where(ProcessRun.task_id == task_id)
        .order_by(ProcessRun.started_at.desc())
        .limit(1)
    )
    run = pr_result.scalar_one_or_none()
    if run:
        run.current_state = "canceled"
        run.ended_at = datetime.utcnow()
        run.lease_owner = None
    db.add(
        Event(
            correlation_id=task.correlation_id or task_id,
            entity_type="task",
            entity_id=task_id,
            event_type="task.cancelled",
            event_payload={"source": "api"},
        )
    )
    await db.commit()
    return {"status": "cancelled", "task_id": task_id}


@app.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, db: AsyncSession = Depends(get_db)):
    """Re-run a failed or stopped task with a fresh workflow (dashboard manual retry)."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Task not found")
    if source.status in ("running", "pending_user_input", "created"):
        raise HTTPException(status_code=409, detail="Task is still active")

    catalog_types = set(CATALOG.keys())
    new_id = str(uuid.uuid4())
    idem_key = hashlib.sha256(f"retry:{task_id}:{time.time()}".encode()).hexdigest()
    task = Task(
        id=new_id,
        correlation_id=source.correlation_id or task_id,
        idempotency_key=idem_key,
        requester=source.requester,
        openclaw_session_key=source.openclaw_session_key,
        task_type=source.task_type or "user",
        goal=source.goal,
        status="created",
    )
    db.add(task)
    db.add(
        Event(
            correlation_id=task.correlation_id or new_id,
            entity_type="task",
            entity_id=new_id,
            event_type="task.retried",
            event_payload={"source_task_id": task_id},
        )
    )
    await db.commit()
    metrics_inc("task_created")

    try:
        await _start_task_workflow(
            new_id,
            source.goal or "",
            source.openclaw_session_key or "agent:main:main",
            source.task_type or "user",
            correlation_id=source.correlation_id or task_id,
            process_type=source.task_type if source.task_type in catalog_types else None,
        )
    except Exception as e:
        task.status = "failed"
        metrics_inc("task_failed")
        await db.commit()
        from app.production.alerting import send_alert

        await send_alert(
            "task.failed",
            f"Retry workflow start failed for task {new_id[:8]}",
            severity="error",
            context={"task_id": new_id, "retried_from": task_id, "error": str(e)[:300]},
        )
        raise HTTPException(status_code=500, detail=f"Workflow start failed: {str(e)}")

    return {"task_id": new_id, "status": "created", "retried_from": task_id}


@app.post("/dev/suspend-all")
async def suspend_all_running(db: AsyncSession = Depends(get_db)):
    """Terminate all running Temporal workflows and mark tasks stopped."""
    client = await connect_temporal()
    terminated = 0
    async for wf in client.list_workflows('ExecutionStatus="Running"'):
        try:
            handle = client.get_workflow_handle(wf.id, run_id=wf.run_id)
            await handle.terminate("Development suspend-all")
            terminated += 1
        except Exception as e:
            logger.warning("Failed to terminate %s: %s", wf.id, e)

    result = await db.execute(
        select(Task).where(Task.status.in_(["running", "pending_user_input", "created"]))
    )
    stopped = 0
    for task in result.scalars().all():
        task.status = "stopped_by_user"
        stopped += 1
    await db.commit()

    settings = load_settings()
    settings["development_mode"] = True
    settings["suspend_slack_notifications"] = True
    settings["suspend_task_interception"] = True
    settings["intermediate_updates"] = False
    save_settings(settings)

    return {"terminated_workflows": terminated, "stopped_tasks": stopped, "development_mode": True}


@app.post("/tasks/{task_id}/signal")
async def signal_task(
    task_id: str, signal: SignalRequest, db: AsyncSession = Depends(get_db)
):
    from app.task_registry.messages import add_task_message

    if signal.message:
        await add_task_message(
            task_id,
            signal.message,
            role="user",
            source="signal",
            db=db,
        )
        await db.commit()
    try:
        client = await connect_temporal()
        handle = client.get_workflow_handle(f"workflow-{task_id}")
        st = signal.signal_type.lower()
        if st == "cancel":
            await handle.signal("cancel", signal.message or "Cancelled")
        elif st == "approve":
            await handle.signal("approve", signal.message or "approved")
        elif st == "retry":
            await handle.signal("retry")
        else:
            await handle.signal("user_input", signal.message)
        return {"status": "signaled", "task_id": task_id, "signal_type": st}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tasks/{task_id}/approve")
async def approve_task(task_id: str, signal: SignalRequest):
    signal.signal_type = "approve"
    return await signal_task(task_id, signal)


@app.get("/sessions/{session_key:path}/active_task")
async def get_active_task(session_key: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Task)
        .where(
            Task.openclaw_session_key == session_key,
            Task.status.in_(["running", "pending_user_input", "created"]),
        )
        .order_by(Task.created_at.desc())
        .limit(1)
    )
    task = result.scalar_one_or_none()
    if not task:
        return {"active_task": None}
    return {
        "active_task": {"id": task.id, "status": task.status, "goal": task.goal}
    }


@app.get("/sessions/{session_key:path}/active_user_task")
async def get_active_user_task(session_key: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Task)
        .where(
            Task.openclaw_session_key == session_key,
            Task.status.in_(["running", "pending_user_input", "created"]),
            Task.task_type.notin_(["cron", "heartbeat"]),
        )
        .order_by(Task.created_at.desc())
        .limit(1)
    )
    task = result.scalar_one_or_none()
    if not task:
        return {"active_task": None}
    return {
        "active_task": {"id": task.id, "status": task.status, "goal": task.goal}
    }


@app.post("/memory/write")
async def memory_write(req: MemoryWriteRequest):
    mem_id = await MemoryRouter.write(
        req.scope_type,
        req.scope_id,
        req.memory_type,
        req.content,
        confidence=req.confidence,
    )
    return {"memory_id": mem_id}


@app.post("/memory/lookup")
async def memory_lookup(req: MemoryLookupRequest):
    items = await MemoryRouter.read(
        req.scope_type,
        req.scope_id,
        req.memory_type,
        req.limit,
        query=req.query,
    )
    return {"items": items}


@app.get("/memory/vector/status")
async def memory_vector_status():
    status = await MemoryRouter.vector_status()
    return status


@app.post("/memory/compact")
async def memory_compact(req: MemoryCompactRequest):
    return await MemoryRouter.compact_episodic_memory(req.max_age_days)


@app.get("/memory/process/{process_run_id}/context")
async def memory_process_context(
    process_run_id: str,
    task_id: Optional[str] = None,
    process_type: str = "generic",
    query: Optional[str] = None,
    user_scope_id: str = "default",
):
    try:
        block = await MemoryRouter.build_context_block(
            process_run_id=process_run_id,
            query=query,
            task_id=task_id,
            process_type=process_type,
            user_scope_id=user_scope_id,
        )
        items = await MemoryRouter.read_ordered(
            process_run_id=process_run_id,
            task_id=task_id,
            user_scope_id=user_scope_id,
            process_type=process_type,
            query=query,
            limit=20,
        )
        return {"context_block": block, "items": items, "count": len(items)}
    except Exception as exc:
        logger.warning("memory_process_context fail-soft: %s", exc)
        return {"context_block": "", "items": [], "count": 0, "warning": str(exc)[:200]}


class MemoryLinkRequest(BaseModel):
    source_id: str
    target_id: str
    relation: str


@app.post("/memory/link")
async def memory_link(body: MemoryLinkRequest):
    from app.memory.graph import link_memory

    link_id = await link_memory(body.source_id, body.target_id, body.relation)
    return {"link_id": link_id}


@app.delete("/memory/link")
async def memory_unlink(body: MemoryLinkRequest):
    from app.memory.graph import unlink_memory

    removed = await unlink_memory(body.source_id, body.target_id, body.relation)
    return {"removed": removed}


@app.get("/memory/links/{memory_id}")
async def memory_links(memory_id: str, relation: Optional[str] = None):
    from app.memory.graph import query_links

    return {"links": await query_links(memory_id, relation=relation)}


@app.post("/artifacts/register")
async def artifacts_register(req: ArtifactRegisterRequest):
    from app.artifacts.store import ArtifactStore, decode_content
    from app.config import is_artifact_store_enabled

    if not is_artifact_store_enabled():
        raise HTTPException(status_code=503, detail="Artifact store disabled")
    data = decode_content(req.content, req.content_encoding)
    filename = req.filename or f"{req.kind}.bin"
    try:
        return await ArtifactStore.store(
            process_run_id=req.process_run_id,
            kind=req.kind,
            data=data,
            filename=filename,
            mime_type=req.mime_type,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/artifacts/process/{process_run_id}")
async def artifacts_list(process_run_id: str):
    from app.artifacts.store import ArtifactStore

    return {"artifacts": await ArtifactStore.list_for_process(process_run_id)}


@app.get("/artifacts/{artifact_id}")
async def artifacts_get(artifact_id: str):
    from app.artifacts.store import ArtifactStore

    meta = await ArtifactStore.get_metadata(artifact_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return meta


@app.get("/artifacts/{artifact_id}/download")
async def artifacts_download(artifact_id: str):
    from app.artifacts.store import ArtifactStore

    meta = await ArtifactStore.get_metadata(artifact_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Artifact not found")
    try:
        data = await ArtifactStore.read_content(artifact_id, verify=True)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return Response(
        content=data,
        media_type=meta.get("mime_type") or "application/octet-stream",
        headers={
            "X-Artifact-Checksum": meta["checksum"],
            "X-Artifact-Checksum-Alg": "sha256",
            "ETag": f'"{meta["checksum"]}"',
            "Content-Disposition": f'inline; filename="{meta.get("filename", "artifact")}"',
        },
    )


@app.get("/settings")
async def get_settings():
    return load_settings()


@app.post("/settings")
async def update_settings(req: SettingsRequest):
    settings = load_settings()
    if req.intermediate_updates is not None:
        settings["intermediate_updates"] = req.intermediate_updates
    if req.development_mode is not None:
        settings["development_mode"] = req.development_mode
    if req.suspend_slack_notifications is not None:
        settings["suspend_slack_notifications"] = req.suspend_slack_notifications
    if req.suspend_task_interception is not None:
        settings["suspend_task_interception"] = req.suspend_task_interception
    save_settings(settings)
    return {"status": "ok", "settings": settings}


# --- Dashboard (unchanged logic, condensed) ---

def _read_current_model() -> dict:
    import json

    result = {"model": "unknown", "provider": "unknown", "api_key_hint": ""}
    try:
        with open(OPENCLAW_CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        primary = (
            cfg.get("agents", {})
            .get("defaults", {})
            .get("model", {})
            .get("primary", "unknown")
        )
        result["model"] = primary
        if "/" in primary:
            result["provider"] = primary.split("/")[0]
    except Exception:
        pass
    return result


@app.get("/api/scanners/catalog")
async def scanners_catalog():
    return {"scanners": list_scanners()}


@app.get("/api/scanners/status")
async def scanners_status():
    scanners = await get_scanner_status()
    running = sum(1 for s in scanners if s["status"] == "running")
    stale = sum(1 for s in scanners if s["status"] == "stale")
    stopped = sum(1 for s in scanners if s["status"] == "stopped")
    unknown = sum(1 for s in scanners if s["status"] == "unknown")
    return {
        "scanners": scanners,
        "summary": {
            "total": len(scanners),
            "running": running,
            "stale": stale,
            "stopped": stopped,
            "unknown": unknown,
        },
    }


@app.post("/api/scanners/sync")
async def scanners_sync():
    stats = await sync_scanners_once()
    scanners = await get_scanner_status()
    return {"sync": stats, "scanners": scanners}


@app.get("/api/workflow-catalog")
async def workflow_catalog():
    return {"templates": list_catalog()}


@app.get("/api/llm/usage")
async def llm_usage_summary():
    from app.llm.usage_monitor import get_summary

    return get_summary()


@app.get("/api/llm/orchestration")
async def llm_orchestration_status():
    from app.llm.quota_broker import get_orchestration_status

    return get_orchestration_status(load_settings())


class LlmReserveRequest(BaseModel):
    session_key: Optional[str] = None


class LlmReleaseRequest(BaseModel):
    session_key: Optional[str] = None
    slot_id: Optional[str] = None


class LlmRecordGatewayRequest(BaseModel):
    session_key: Optional[str] = None
    profile_id: Optional[str] = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@app.post("/api/llm/reserve")
async def llm_reserve(body: LlmReserveRequest):
    from app.llm.quota_broker import get_orchestration_status, reserve_profile

    try:
        profile_id, slot_id = await reserve_profile(
            session_key=body.session_key,
            settings=load_settings(),
        )
    except TimeoutError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {
        "profile_id": profile_id,
        "slot_id": slot_id,
        "orchestration": get_orchestration_status(load_settings()),
    }


@app.post("/api/llm/release")
async def llm_release(body: LlmReleaseRequest):
    from app.llm.quota_broker import get_orchestration_status, release_profile

    released = await release_profile(
        session_key=body.session_key,
        slot_id=body.slot_id,
    )
    return {
        "released": released,
        "orchestration": get_orchestration_status(load_settings()),
    }


@app.post("/api/llm/record-gateway")
async def llm_record_gateway(body: LlmRecordGatewayRequest):
    from app.llm.quota_broker import profile_for_session
    from app.llm.usage_monitor import record_request

    profile_id = body.profile_id
    if not profile_id and body.session_key:
        profile_id = profile_for_session(body.session_key)
    if not profile_id and body.session_key:
        from app.llm.usage_monitor import _profile_for_session_key

        profile_id = _profile_for_session_key(body.session_key)
    profile_id = profile_id or "nvidia:unknown"
    record_request(
        profile_id,
        "openclaw_llm",
        input_tokens=body.input_tokens,
        output_tokens=body.output_tokens,
        total_tokens=body.total_tokens or (body.input_tokens + body.output_tokens),
        model=body.model,
    )
    return {"recorded": True, "profile_id": profile_id}


@app.get("/api/production/readiness")
async def production_readiness():
    return await run_all_checks()


@app.get("/api/cron/jobs")
async def cron_jobs_snapshot():
    from app.cron.reconciler import load_openclaw_cron_jobs

    return {"jobs": load_openclaw_cron_jobs()}


@app.post("/api/cron/reconcile")
async def cron_reconcile(db: AsyncSession = Depends(get_db)):
    return await reconcile_cron_once(db)


@app.get("/api/dashboard-data")
async def dashboard_data(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Task).order_by(Task.created_at.desc()).limit(50))
    tasks = result.scalars().all()
    task_list = [
        {
            "id": t.id,
            "goal": (t.goal or "")[:100],
            "session_key": t.openclaw_session_key or "",
            "status": t.status or "unknown",
            "task_type": t.task_type or "user",
            "created_at": t.created_at.isoformat() if t.created_at else "",
        }
        for t in tasks
    ]
    return {
        "tasks": task_list,
        "stats": {
            "total": len(tasks),
            "running": sum(1 for t in tasks if t.status == "running"),
            "completed": sum(1 for t in tasks if t.status == "completed"),
            "failed": sum(1 for t in tasks if t.status == "failed"),
        },
        "model": _read_current_model(),
        "model_catalog": MODEL_CATALOG,
        "settings": load_settings(),
        "workflow_catalog": list_catalog(),
        "vector_memory": await MemoryRouter.vector_status(),
        "telemetry": telemetry_status(),
        "scanners": await get_scanner_status(),
        "readiness": await run_all_checks(),
        "last_backup": get_last_backup_info(),
        "metrics": get_counters(),
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Task).order_by(Task.created_at.desc()).limit(50))
    tasks = result.scalars().all()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "tasks": tasks,
            "model": _read_current_model(),
            "model_catalog": MODEL_CATALOG,
            "settings": load_settings(),
        },
    )


@app.post("/config")
async def update_config(config: ConfigRequest):
    import json

    try:
        with open(OPENCLAW_CONFIG_PATH, "r") as f:
            cfg = json.load(f)
    except Exception:
        raise HTTPException(status_code=500, detail="Cannot read openclaw.json")

    provider = config.provider.strip()
    model_name = config.model.strip()
    if not provider or not model_name:
        raise HTTPException(status_code=400, detail="Provider and model required")

    model_id = f"{provider}/{model_name}" if "/" not in model_name else model_name
    cfg.setdefault("agents", {}).setdefault("defaults", {}).setdefault("model", {})[
        "primary"
    ] = model_id

    with open(OPENCLAW_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

    if config.api_key:
        try:
            with open(AUTH_PROFILES_PATH, "r") as f:
                auth = json.load(f)
        except Exception:
            auth = {"version": 1, "profiles": {}}
        profile_key = f"{provider}:default"
        auth.setdefault("profiles", {})[profile_key] = {
            "provider": provider,
            "type": "api_key",
            "key": config.api_key.strip(),
        }
        with open(AUTH_PROFILES_PATH, "w") as f:
            json.dump(auth, f, indent=2)

    subprocess.Popen(
        "pkill -f 'openclaw-gateway' 2>/dev/null; sleep 2; "
        "nohup openclaw gateway --force > /tmp/openclaw-gateway.log 2>&1 &",
        shell=True,
    )
    return {"status": "ok", "model": model_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
