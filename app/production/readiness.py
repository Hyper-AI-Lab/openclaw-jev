"""Production readiness checks — gate for unsupervised go-live."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

from app.config import (
    OPENCLAW_CONFIG_PATH,
    RMP_DATA_DIR,
    SETTINGS_PATH,
    get_openclaw_hook_token,
    get_openclaw_url,
    get_slack_bot_token,
    is_artifact_store_enabled,
    is_development_mode,
    is_telemetry_enabled,
    is_vector_memory_enabled,
    load_settings,
)

RMP_API = "http://127.0.0.1:8000"
TEMPORAL_ADDR = "localhost:7233"
SYSTEMD_UNITS = [
    "temporal.service",
    "rmp-api.service",
    "rmp-worker.service",
    "openclaw-gateway.service",
    "rmp-qdrant.service",
]
BACKUP_ROOT = os.path.join(RMP_DATA_DIR, "backups")
MEMORY_CANARY_RESULT_PATH = os.path.join(RMP_DATA_DIR, "last_memory_canary.json")
HEALTH_CANARY_RESULT_PATH = os.path.join(RMP_DATA_DIR, "last_health_canary.json")


@dataclass
class CheckResult:
    name: str
    status: str  # pass | warn | fail
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


def _systemd_active(unit: str) -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def _read_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


async def check_systemd_services() -> CheckResult:
    missing = [u for u in SYSTEMD_UNITS if not _systemd_active(u)]
    if missing:
        return CheckResult(
            "systemd_services",
            "fail",
            f"Inactive units: {', '.join(missing)}",
            {"inactive": missing},
        )
    return CheckResult("systemd_services", "pass", "All core systemd units active")


async def check_rmp_health() -> CheckResult:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            settings = load_settings()
            headers = {"X-RMP-API-Key": settings.get("api_key", "")}
            r = await client.get(f"{RMP_API}/health", headers=headers)
            if r.status_code != 200:
                return CheckResult(
                    "rmp_health",
                    "fail",
                    f"Health returned {r.status_code}",
                    {"body": r.text[:500]},
                )
            body = r.json()
            if body.get("status") != "ok":
                return CheckResult("rmp_health", "warn", "Health non-ok", body)
            return CheckResult("rmp_health", "pass", "RMP health OK", body)
    except Exception as e:
        return CheckResult("rmp_health", "fail", str(e))


async def check_temporal() -> CheckResult:
    try:
        from temporalio.client import Client

        client = await Client.connect(TEMPORAL_ADDR)
        await client.service_client.check_health()
        return CheckResult("temporal", "pass", "Temporal reachable")
    except Exception as e:
        return CheckResult("temporal", "fail", f"Temporal unreachable: {e}")


async def check_openclaw_gateway() -> CheckResult:
    url = get_openclaw_url()
    token = get_openclaw_hook_token()
    if not token:
        return CheckResult("openclaw_gateway", "fail", "Hook token not configured")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{url}/health",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 200:
                return CheckResult("openclaw_gateway", "pass", "OpenClaw gateway OK")
            return CheckResult(
                "openclaw_gateway",
                "warn",
                f"Gateway returned {r.status_code}",
                {"status_code": r.status_code},
            )
    except Exception as e:
        return CheckResult("openclaw_gateway", "fail", str(e))


def check_development_mode() -> CheckResult:
    if is_development_mode():
        return CheckResult(
            "development_mode",
            "warn",
            "development_mode is ON — not production",
            {"development_mode": True},
        )
    return CheckResult("development_mode", "pass", "Production mode active")


def check_slack_configured() -> CheckResult:
    token = get_slack_bot_token()
    if not token:
        return CheckResult("slack", "fail", "Slack bot token missing in openclaw.json")
    return CheckResult("slack", "pass", "Slack bot token present")


def gateway_process_pids() -> List[int]:
    """PIDs of openclaw-gateway on this host (systemd MainPID + /proc comm)."""
    pids: List[int] = []
    try:
        raw = subprocess.check_output(
            [
                "systemctl",
                "show",
                "-p",
                "MainPID",
                "--value",
                "openclaw-gateway.service",
            ],
            text=True,
            timeout=5,
        ).strip()
        if raw.isdigit() and int(raw) > 0:
            pids.append(int(raw))
    except Exception:
        pass
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            comm_path = os.path.join("/proc", name, "comm")
            try:
                with open(comm_path, "r", encoding="utf-8") as handle:
                    comm = handle.read().strip()
            except OSError:
                continue
            if comm == "openclaw-gateway":
                pids.append(int(name))
    except OSError:
        pass
    return sorted(set(pids))


def check_slack_sockets() -> CheckResult:
    """Detect duplicate Slack Socket Mode gateways on this machine only."""
    pids = gateway_process_pids()
    n = len(pids)
    details = {"gateway_pids": pids, "count": n}
    if n == 1:
        return CheckResult(
            "slack_sockets",
            "pass",
            "Single OpenClaw gateway on this host",
            details,
        )
    if n == 0:
        return CheckResult(
            "slack_sockets",
            "warn",
            "No openclaw-gateway process on this host",
            details,
        )
    return CheckResult(
        "slack_sockets",
        "warn",
        f"{n} openclaw-gateway processes on this host (dual Slack socket risk)",
        details,
    )


def check_api_key() -> CheckResult:
    key = load_settings().get("api_key", "")
    if len(key) < 32:
        return CheckResult("api_key", "fail", "API key missing or too short")
    return CheckResult("api_key", "pass", "API key configured")


def get_last_backup_info() -> Dict[str, Any]:
    """Latest backup directory metadata for dashboards."""
    if not os.path.isdir(BACKUP_ROOT):
        return {"found": False, "path": BACKUP_ROOT}
    latest: Optional[datetime] = None
    latest_name = ""
    for name in os.listdir(BACKUP_ROOT):
        path = os.path.join(BACKUP_ROOT, name)
        if os.path.isdir(path):
            mtime = datetime.utcfromtimestamp(os.path.getmtime(path))
            if latest is None or mtime > latest:
                latest = mtime
                latest_name = name
    if latest is None:
        return {"found": False, "path": BACKUP_ROOT}
    manifest = _read_json(os.path.join(BACKUP_ROOT, latest_name, "manifest.json"))
    age_hours = (datetime.utcnow() - latest).total_seconds() / 3600
    return {
        "found": True,
        "id": latest_name,
        "timestamp": latest.isoformat() + "Z",
        "age_hours": round(age_hours, 2),
        "manifest": manifest,
        "path": os.path.join(BACKUP_ROOT, latest_name),
    }


def readiness_score(summary: Dict[str, int]) -> int:
    """Simple 0–100 score from pass/warn/fail counts."""
    total = sum(summary.values())
    if total == 0:
        return 0
    return int(round(100 * summary.get("pass", 0) / total))


def check_backup_recency(max_hours: int = 26) -> CheckResult:
    if not os.path.isdir(BACKUP_ROOT):
        return CheckResult(
            "backups",
            "warn",
            "No backup directory yet — run ops/backup.sh",
            {"path": BACKUP_ROOT},
        )
    latest: Optional[datetime] = None
    latest_name = ""
    for name in os.listdir(BACKUP_ROOT):
        path = os.path.join(BACKUP_ROOT, name)
        if os.path.isdir(path):
            mtime = datetime.utcfromtimestamp(os.path.getmtime(path))
            if latest is None or mtime > latest:
                latest = mtime
                latest_name = name
    if latest is None:
        return CheckResult("backups", "warn", "No backups found")
    age = datetime.utcnow() - latest
    if age > timedelta(hours=max_hours):
        return CheckResult(
            "backups",
            "warn",
            f"Latest backup {latest_name} is {age.total_seconds() / 3600:.1f}h old",
            {"latest": latest_name, "age_hours": age.total_seconds() / 3600},
        )
    return CheckResult(
        "backups",
        "pass",
        f"Latest backup {latest_name} ({age.total_seconds() / 3600:.1f}h ago)",
        {"latest": latest_name},
    )


def _temporal_journal_mode(db_path: str) -> str:
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute("PRAGMA journal_mode").fetchone()
            return str(row[0]).lower() if row else ""
        finally:
            conn.close()
    except Exception:
        return ""


def _temporal_postgres_schemas() -> Dict[str, bool]:
    """True when the official server's databases accept a connection."""
    password = os.environ.get("TEMPORAL_POSTGRES_PWD", "")
    if not password:
        return {"temporal": False, "temporal_visibility": False}
    try:
        import psycopg2
    except Exception:
        return {"temporal": False, "temporal_visibility": False}
    found: Dict[str, bool] = {}
    for name in ("temporal", "temporal_visibility"):
        try:
            conn = psycopg2.connect(
                host="127.0.0.1",
                port=5432,
                dbname=name,
                user="temporal",
                password=password,
                connect_timeout=3,
            )
            conn.close()
            found[name] = True
        except Exception:
            found[name] = False
    return found


def check_temporal_persistence() -> CheckResult:
    svc_path = "/etc/systemd/system/temporal.service"
    try:
        with open(svc_path) as f:
            content = f.read()
    except OSError as exc:
        return CheckResult(
            "temporal_persistence",
            "warn",
            f"Temporal unit missing: {exc}",
        )
    if "start-dev" in content:
        return CheckResult(
            "temporal_persistence",
            "warn",
            "Temporal unit still runs start-dev",
        )
    if "postgres12" not in content:
        return CheckResult(
            "temporal_persistence",
            "warn",
            "Temporal unit is not the Postgres server",
        )
    schemas = _temporal_postgres_schemas()
    if schemas.get("temporal") and schemas.get("temporal_visibility"):
        return CheckResult(
            "temporal_persistence",
            "pass",
            "Temporal Postgres schemas reachable",
            schemas,
        )
    return CheckResult(
        "temporal_persistence",
        "warn",
        "Temporal Postgres schemas not reachable",
        schemas,
    )


def check_telemetry_export() -> CheckResult:
    if not is_telemetry_enabled():
        return CheckResult("telemetry", "warn", "Telemetry disabled")
    endpoint = load_settings().get("telemetry", {}).get("otlp_endpoint", "")
    if not endpoint:
        return CheckResult(
            "telemetry",
            "warn",
            "OTLP endpoint not configured — traces in-process only",
        )
    return CheckResult("telemetry", "pass", f"OTLP export configured: {endpoint}")


def check_vector_memory() -> CheckResult:
    if not is_vector_memory_enabled():
        cfg = load_settings().get("vector_memory", {})
        reason = cfg.get("not_ready_reason") or "Vector memory disabled"
        return CheckResult(
            "vector_memory",
            "warn",
            reason,
            {
                "enabled": False,
                "ready": False,
                "embedder_model": cfg.get("embedder_model"),
            },
        )
    cfg = load_settings().get("vector_memory", {})
    mode = (cfg.get("qdrant_mode") or "embedded").strip().lower()
    try:
        from app.memory.vector import get_vector_service

        st = get_vector_service().status()
        details = {k: v for k, v in st.items() if k != "error"}
        if st.get("ready"):
            msg = f"Vector memory ready ({mode})"
            if mode == "server":
                msg += f" @ {st.get('qdrant_host')}:{st.get('qdrant_port')}"
            return CheckResult("vector_memory", "pass", msg, details)
        err = st.get("error") or "not initialized"
        return CheckResult(
            "vector_memory",
            "warn",
            f"Vector memory not ready ({mode}): {err}",
            details,
        )
    except Exception as exc:
        return CheckResult("vector_memory", "fail", str(exc)[:200])


def check_artifact_store() -> CheckResult:
    if not is_artifact_store_enabled():
        return CheckResult("artifact_store", "warn", "Artifact store disabled")
    root = load_settings().get("artifact_store", {}).get("root_path", "")
    if root and os.path.isdir(root):
        return CheckResult("artifact_store", "pass", "Artifact store ready", {"path": root})
    return CheckResult("artifact_store", "fail", "Artifact root missing", {"path": root})


def check_pg_dump_available() -> CheckResult:
    if shutil.which("pg_dump"):
        return CheckResult("pg_dump", "pass", "pg_dump available")
    return CheckResult("pg_dump", "fail", "pg_dump not found — backups will fail")


def check_heartbeat_config() -> CheckResult:
    cfg = _read_json(OPENCLAW_CONFIG_PATH)
    hb = cfg.get("agents", {}).get("defaults", {}).get("heartbeat", {})
    every = hb.get("every", "")
    if is_development_mode():
        return CheckResult(
            "heartbeat",
            "warn",
            f"Dev mode — heartbeat every={every!r}",
            {"every": every},
        )
    if every in ("0", 0, "", None):
        return CheckResult(
            "heartbeat",
            "warn",
            "Heartbeat disabled — reconciler-only liveness",
            {"every": every},
        )
    return CheckResult("heartbeat", "pass", f"Heartbeat every={every}", {"every": every})


def check_health_canary_recency(max_hours: int = 2) -> CheckResult:
    data = _read_json(HEALTH_CANARY_RESULT_PATH)
    if not data:
        return CheckResult(
            "health_canary",
            "warn",
            "No last_health_canary.json — hourly canary may not have run",
            {"path": HEALTH_CANARY_RESULT_PATH},
        )
    finished = data.get("finished_at") or data.get("timestamp")
    if not finished:
        return CheckResult("health_canary", "warn", "Health canary result missing finished_at")
    try:
        ts = datetime.fromisoformat(finished.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return CheckResult("health_canary", "warn", "Invalid health canary timestamp")
    age = datetime.utcnow() - ts
    if data.get("status") != "completed":
        return CheckResult(
            "health_canary",
            "warn",
            f"Last health canary status={data.get('status')}",
            data,
        )
    if age > timedelta(hours=max_hours):
        return CheckResult(
            "health_canary",
            "warn",
            f"Last health canary {age.total_seconds() / 3600:.1f}h old",
            {"age_hours": age.total_seconds() / 3600},
        )
    return CheckResult(
        "health_canary",
        "pass",
        f"Health canary OK ({age.total_seconds() / 3600:.1f}h ago)",
        {"task_id": data.get("task_id")},
    )


def check_memory_canary_recency(max_hours: int = 25) -> CheckResult:
    data = _read_json(MEMORY_CANARY_RESULT_PATH)
    if not data:
        return CheckResult(
            "memory_canary",
            "warn",
            "No last_memory_canary.json — run ops/canary_slack_memory.sh",
            {"path": MEMORY_CANARY_RESULT_PATH},
        )
    finished = data.get("finished_at") or data.get("timestamp")
    if not finished:
        return CheckResult("memory_canary", "warn", "Canary result missing finished_at")
    try:
        ts = datetime.fromisoformat(finished.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return CheckResult("memory_canary", "warn", "Invalid canary timestamp")
    age = datetime.utcnow() - ts
    if data.get("status") == "inconclusive":
        return CheckResult(
            "memory_canary",
            "warn",
            "Last memory canary inconclusive (transcript missing)",
            data,
        )
    if data.get("status") != "completed":
        return CheckResult(
            "memory_canary",
            "warn",
            f"Last memory canary status={data.get('status')}",
            data,
        )
    if not data.get("memory_ok") or not data.get("prompt_ok"):
        return CheckResult(
            "memory_canary",
            "warn",
            "Last memory canary completed but memory/prompt not proven",
            data,
        )
    if age > timedelta(hours=max_hours):
        return CheckResult(
            "memory_canary",
            "warn",
            f"Last memory canary {age.total_seconds() / 3600:.1f}h old",
            {"age_hours": age.total_seconds() / 3600},
        )
    return CheckResult(
        "memory_canary",
        "pass",
        f"Memory canary OK ({age.total_seconds() / 3600:.1f}h ago)",
        {"task_id": data.get("task_id")},
    )


async def check_stuck_workflows(max_count: int = 3) -> CheckResult:
    from app.reconciler import count_stuck_running_workflows

    count = await count_stuck_running_workflows()
    if count < 0:
        return CheckResult("stuck_workflows", "warn", "Could not count stuck workflows")
    if count > max_count:
        return CheckResult(
            "stuck_workflows",
            "warn",
            f"{count} stuck RUNNING workflows (threshold {max_count})",
            {"count": count},
        )
    return CheckResult(
        "stuck_workflows",
        "pass",
        f"Stuck workflow count OK ({count})",
        {"count": count},
    )


def check_openai_key() -> CheckResult:
    """Warn when OPENAI_API_KEY is unset — primary stays gpt-5-nano; NVIDIA fallbacks still work."""
    from app.llm.model_policy import openai_key_present

    if openai_key_present():
        return CheckResult(
            "openai_key",
            "pass",
            "OPENAI_API_KEY present (gpt-5-nano primary wired)",
        )
    return CheckResult(
        "openai_key",
        "warn",
        "openai_key_missing — primary openai/gpt-5-nano unwired until OPENAI_API_KEY is set in /etc/openclaw/openclaw.env",
    )


def check_task_registry_config() -> CheckResult:
    from app.config import get_task_registry_config, get_task_registry_intake_mode

    cfg = get_task_registry_config()
    if not cfg.get("enabled", True):
        return CheckResult("task_registry", "warn", "Task registry disabled")
    mode = get_task_registry_intake_mode()
    return CheckResult(
        "task_registry",
        "pass",
        f"Task registry enabled (intake_mode={mode})",
        {"collection": cfg.get("collection_name"), "intake_mode": mode},
    )


def check_task_registry_vector() -> CheckResult:
    from app.config import get_task_registry_config, is_vector_memory_enabled

    cfg = get_task_registry_config()
    if not cfg.get("enabled", True):
        return CheckResult("task_registry_vector", "warn", "Task registry disabled")
    if not is_vector_memory_enabled():
        return CheckResult(
            "task_registry_vector",
            "warn",
            "Task registry dense index skipped (embedder not-ready)",
        )
    try:
        from app.task_registry.vector_store import probe_task_registry_vector

        probe = probe_task_registry_vector()
        latency_ms = int(probe.get("latency_ms") or 0)
        if not probe.get("ok"):
            return CheckResult(
                "task_registry_vector",
                "fail",
                probe.get("message", "vector probe failed"),
            )
        if latency_ms > 5000:
            return CheckResult(
                "task_registry_vector",
                "warn",
                f"Task registry vector slow ({latency_ms}ms)",
                {"latency_ms": latency_ms},
            )
        return CheckResult(
            "task_registry_vector",
            "pass",
            f"Task registry vector OK ({latency_ms}ms)",
            {"latency_ms": latency_ms},
        )
    except Exception as exc:
        return CheckResult(
            "task_registry_vector",
            "fail",
            str(exc)[:200],
        )


def check_task_registry_index_fresh() -> CheckResult:
    """Warn if terminal tasks significantly outnumber indexed registry entries."""
    from datetime import datetime, timedelta

    from sqlalchemy import create_engine, func, select, text

    from app.config import get_task_registry_config
    from app.db.database import DATABASE_URL
    from app.db.models import Task, TaskRegistryEntry
    from app.task_registry.indexer import TERMINAL_STATUSES

    cfg = get_task_registry_config()
    if not cfg.get("enabled", True):
        return CheckResult("task_registry_index", "warn", "Task registry disabled")
    days = int(cfg.get("backfill_days", 90))
    cutoff = datetime.utcnow() - timedelta(days=days)
    sync_url = DATABASE_URL.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    terminal_statuses = list(TERMINAL_STATUSES)
    try:
        with engine.connect() as conn:
            terminal = conn.execute(
                text(
                    "SELECT count(*) FROM tasks WHERE status = ANY(:st) AND created_at >= :cutoff"
                ),
                {"st": terminal_statuses, "cutoff": cutoff},
            ).scalar()
            indexed = conn.execute(
                text("SELECT count(*) FROM task_registry_entries")
            ).scalar()
    except Exception as exc:
        return CheckResult(
            "task_registry_index",
            "warn",
            f"Registry index check failed: {exc}",
        )
    terminal_n = int(terminal or 0)
    indexed_n = int(indexed or 0)
    if terminal_n == 0:
        return CheckResult("task_registry_index", "pass", "No terminal tasks to index")
    ratio = indexed_n / terminal_n if terminal_n else 1.0
    if ratio >= 0.85:
        return CheckResult(
            "task_registry_index",
            "pass",
            f"Registry index fresh ({indexed_n}/{terminal_n})",
            {"indexed": indexed_n, "terminal": terminal_n},
        )
    return CheckResult(
        "task_registry_index",
        "warn",
        f"Registry index stale ({indexed_n}/{terminal_n} terminal tasks indexed)",
        {"indexed": indexed_n, "terminal": terminal_n},
    )


def check_safe_harbor_peripheral() -> CheckResult:
    """Safe Harbor is not on the Slack path. Warn if auto-restart is on."""
    from app.config import load_settings

    harbor = os.environ.get("AURA_SAFE_HARBOR", "/root/aura_safe_harbor")
    auto = bool(
        (load_settings().get("production") or {}).get("scanner_auto_restart")
    )
    if auto and not os.path.isdir(harbor):
        return CheckResult(
            "safe_harbor",
            "warn",
            "scanner_auto_restart is on but Safe Harbor path is missing",
            {"path": harbor},
        )
    if auto:
        return CheckResult(
            "safe_harbor",
            "warn",
            "scanner_auto_restart is on; Safe Harbor stays peripheral to Slack",
        )
    return CheckResult(
        "safe_harbor",
        "pass",
        "Safe Harbor peripheral; scanner_auto_restart off",
        {"path": harbor},
    )


def check_runtime_code_sync() -> CheckResult:
    """Fail when app/ code on disk is newer than running API/worker boot stamps."""
    from app.production.runtime_sync import runtime_sync_status

    status = runtime_sync_status()
    overall = status.get("status")
    if overall == "ok":
        return CheckResult(
            "runtime_code_sync",
            "pass",
            "Running API/worker match on-disk code",
            status,
        )
    if overall == "missing":
        return CheckResult(
            "runtime_code_sync",
            "warn",
            "Runtime boot stamps missing — restart rmp-api/rmp-worker after deploy",
            status,
        )
    stale = ", ".join(status.get("stale_services") or []) or "rmp-api/rmp-worker"
    return CheckResult(
        "runtime_code_sync",
        "fail",
        f"On-disk code newer than running process ({stale}) — run: make restart-rmp",
        status,
    )


async def run_all_checks() -> Dict[str, Any]:
    sync_checks = [
        check_development_mode(),
        check_api_key(),
        check_slack_configured(),
        check_slack_sockets(),
        check_backup_recency(),
        check_temporal_persistence(),
        check_telemetry_export(),
        check_vector_memory(),
        check_artifact_store(),
        check_pg_dump_available(),
        check_heartbeat_config(),
        check_health_canary_recency(),
        check_memory_canary_recency(),
        check_task_registry_config(),
        check_task_registry_vector(),
        check_task_registry_index_fresh(),
        check_runtime_code_sync(),
        check_openai_key(),
        check_safe_harbor_peripheral(),
    ]
    async_checks = await asyncio.gather(
        check_systemd_services(),
        check_rmp_health(),
        check_temporal(),
        check_openclaw_gateway(),
        check_stuck_workflows(),
    )
    results: List[CheckResult] = sync_checks + list(async_checks)

    counts = {"pass": 0, "warn": 0, "fail": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    blocking = [r.name for r in results if r.status == "fail"]
    go_live_ready = len(blocking) == 0 and not is_development_mode()

    score = readiness_score(counts)

    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "go_live_ready": go_live_ready,
        "production_ready": len(blocking) == 0,
        "readiness_score": score,
        "summary": counts,
        "blocking_failures": blocking,
        "checks": [
            {
                "name": r.name,
                "status": r.status,
                "message": r.message,
                "details": r.details,
            }
            for r in results
        ],
    }
