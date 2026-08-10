"""Sync Moltbook scanner OS state into RMP tasks/process runs."""
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import Event, OsProcessTrack, ProcessRun, Task
from app.scanners.catalog import CATALOG, get_scanner, list_scanners
from app.scanners.lifecycle import maybe_restart_scanner
from app.scanners.monitor import log_file_mtime, process_start_time, running_by_scanner_id

logger = logging.getLogger("rmp.scanners.sync")

STALE_LOG_HOURS = 48


async def sync_scanners_once() -> Dict[str, Any]:
    """Reconcile catalog scanners with live OS processes and RMP records."""
    from app.config import is_development_mode

    if is_development_mode():
        return {"skipped": True, "reason": "development_mode"}

    running = running_by_scanner_id()
    stats = {
        "running": len(running),
        "catalog_size": len(CATALOG),
        "started": 0,
        "stopped": 0,
        "updated": 0,
        "stale": 0,
        "restarted": 0,
    }
    now = datetime.utcnow()
    stale_threshold = now - timedelta(hours=STALE_LOG_HOURS)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(OsProcessTrack))
        tracks = {t.scanner_id: t for t in result.scalars().all()}

        for scanner_id, definition in CATALOG.items():
            track = tracks.get(scanner_id)
            if not track:
                track = OsProcessTrack(
                    scanner_id=scanner_id,
                    display_name=definition.display_name,
                    script_basename=definition.script_basename,
                    log_path=definition.log_path,
                    status="stopped",
                    run_count=0,
                )
                db.add(track)
                tracks[scanner_id] = track

            proc = running.get(scanner_id)
            log_mtime = log_file_mtime(definition.log_path)
            track.last_log_mtime = log_mtime
            track.last_seen_at = now
            track.log_path = definition.log_path

            if proc:
                if track.pid != proc.pid or track.status != "running":
                    task_id, run_id = await _start_scanner_run(
                        db, scanner_id, definition.display_name, proc.pid, proc.script_path or definition.script_paths[0]
                    )
                    track.task_id = task_id
                    track.process_run_id = run_id
                    track.pid = proc.pid
                    track.status = "running"
                    track.script_path = proc.script_path or definition.script_paths[0]
                    track.last_started_at = process_start_time(proc.pid) or now
                    track.run_count = (track.run_count or 0) + 1
                    stats["started"] += 1
                else:
                    track.status = "running"
                    stats["updated"] += 1
            else:
                if track.status == "running" and track.task_id:
                    await _stop_scanner_run(db, track, reason="process_exit")
                    stats["stopped"] += 1
                elif log_mtime and log_mtime < stale_threshold and track.status != "stale":
                    track.status = "stale"
                    stats["stale"] += 1
                    restart = await maybe_restart_scanner(
                        scanner_id, definition, reason="stale_log"
                    )
                    if restart:
                        stats["restarted"] += 1
                        db.add(
                            Event(
                                correlation_id=track.task_id or scanner_id,
                                entity_type="os_process",
                                entity_id=scanner_id,
                                event_type="scanner.auto_restart",
                                event_payload=restart,
                            )
                        )
                elif track.status == "running":
                    track.status = "stopped"
                    track.pid = None
                    stats["stopped"] += 1
                else:
                    track.status = track.status if track.status in ("stale", "stopped") else "stopped"
                    track.pid = None

        await db.commit()

    return stats


async def _start_scanner_run(
    db,
    scanner_id: str,
    display_name: str,
    pid: int,
    script_path: str,
) -> tuple:
    task_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    db.add(
        Task(
            id=task_id,
            correlation_id=task_id,
            idempotency_key=f"scanner:{scanner_id}:{pid}",
            requester="os_monitor",
            openclaw_session_key="system:moltbook_scanner",
            task_type="moltbook_scanner",
            goal=f"{display_name} (pid {pid})",
            status="running",
        )
    )
    db.add(
        ProcessRun(
            id=run_id,
            task_id=task_id,
            process_type="moltbook_scanner",
            current_state="running",
            success_criteria={"scanner_id": scanner_id, "script": script_path},
            lease_owner=str(pid),
            next_check_at=datetime.utcnow() + timedelta(minutes=5),
        )
    )
    db.add(
        Event(
            correlation_id=task_id,
            entity_type="os_process",
            entity_id=scanner_id,
            event_type="scanner.started",
            event_payload={"pid": pid, "script_path": script_path},
        )
    )
    await db.flush()
    return task_id, run_id


async def _stop_scanner_run(db, track: OsProcessTrack, reason: str = "process_exit") -> None:
    if track.task_id:
        result = await db.execute(select(Task).where(Task.id == track.task_id))
        task = result.scalar_one_or_none()
        if task and task.status == "running":
            task.status = "completed" if reason == "process_exit" else "stopped_by_user"
            task.next_check_at = None

    if track.process_run_id:
        result = await db.execute(
            select(ProcessRun).where(ProcessRun.id == track.process_run_id)
        )
        run = result.scalar_one_or_none()
        if run and run.current_state == "running":
            run.current_state = "completed"
            run.ended_at = datetime.utcnow()
            run.lease_owner = None

    db.add(
        Event(
            correlation_id=track.task_id or track.scanner_id,
            entity_type="os_process",
            entity_id=track.scanner_id,
            event_type="scanner.stopped",
            event_payload={"reason": reason, "pid": track.pid},
        )
    )
    track.status = "stopped"
    track.pid = None


async def get_scanner_status() -> List[Dict[str, Any]]:
    running = running_by_scanner_id()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(OsProcessTrack).order_by(OsProcessTrack.display_name)
        )
        tracks = list(result.scalars().all())

    known_ids = {t.scanner_id for t in tracks}
    rows: List[Dict[str, Any]] = []

    for track in tracks:
        definition = get_scanner(track.scanner_id)
        proc = running.get(track.scanner_id)
        rows.append(
            {
                "scanner_id": track.scanner_id,
                "display_name": track.display_name,
                "status": "running" if proc else track.status,
                "pid": proc.pid if proc else track.pid,
                "task_id": track.task_id,
                "process_run_id": track.process_run_id,
                "script_path": (proc.script_path if proc else track.script_path)
                or (definition.script_paths[0] if definition else None),
                "log_path": track.log_path,
                "last_log_mtime": track.last_log_mtime.isoformat()
                if track.last_log_mtime
                else None,
                "last_started_at": track.last_started_at.isoformat()
                if track.last_started_at
                else None,
                "last_seen_at": track.last_seen_at.isoformat()
                if track.last_seen_at
                else None,
                "run_count": track.run_count or 0,
            }
        )

    for scanner_id, definition in CATALOG.items():
        if scanner_id in known_ids:
            continue
        proc = running.get(scanner_id)
        rows.append(
            {
                "scanner_id": scanner_id,
                "display_name": definition.display_name,
                "status": "running" if proc else "unknown",
                "pid": proc.pid if proc else None,
                "task_id": None,
                "process_run_id": None,
                "script_path": proc.script_path if proc else definition.script_paths[0],
                "log_path": definition.log_path,
                "last_log_mtime": None,
                "last_started_at": None,
                "last_seen_at": None,
                "run_count": 0,
            }
        )

    rows.sort(key=lambda r: r["display_name"])
    return rows


async def scanner_monitor_loop(stop_event):
    import asyncio

    interval = 60
    logger.info("Scanner monitor started (interval=%ss)", interval)
    while not stop_event.is_set():
        try:
            stats = await sync_scanners_once()
            if stats.get("started") or stats.get("stopped"):
                logger.info("Scanner sync: %s", stats)
        except Exception as e:
            logger.exception("Scanner sync error: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
