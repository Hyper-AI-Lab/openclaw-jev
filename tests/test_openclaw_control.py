"""Stopping Aura's in-flight runs: sessions.abort per session, through a fake openclaw CLI."""
import asyncio
import json
import stat
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app import openclaw_control, openclaw_sessions, temporal_control
from app.db.models import Base, Event

TID = "23c46432-dc11-450d-a160-ab180b1fdbb1"
REAL_ABORT = openclaw_control.abort_session


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    """An openclaw executable that logs its arguments and answers like the gateway."""
    log = tmp_path / "calls.jsonl"
    script = tmp_path / "openclaw"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "params = json.loads(sys.argv[sys.argv.index('--params') + 1])\n"
        "key = params['key']\n"
        "if key.endswith('__v1'):\n"
        "    sys.stderr.write('gateway unreachable'); sys.exit(1)\n"
        "if key.endswith('__r2'):\n"
        "    time.sleep(5)\n"
        "status = 'aborted' if key.endswith(('rmp_task_%s' % key.split('rmp_task_')[-1].split('__')[0],)) else 'no-active-run'\n"
        "print(json.dumps({'ok': True, 'abortedRunId': None, 'status': status}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(openclaw_control.shutil, "which", lambda name: str(script))
    monkeypatch.setattr(openclaw_control, "abort_session", REAL_ABORT)
    monkeypatch.setattr(openclaw_control, "CLI_TIMEOUT_SEC", 2)
    return log


@pytest.fixture
async def sessions(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'abort.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(openclaw_control, "AsyncSessionLocal", maker)
    yield maker
    await engine.dispose()


async def test_every_session_of_the_task_is_aborted_and_queued_turns_cleared(fake_cli, sessions, monkeypatch):
    keys = [f"agent:main:rmp_task_{TID}", f"agent:main:rmp_task_{TID}__r2",
            f"agent:main:rmp_verify_{TID}__v1", f"agent:main:rmp_verify_{TID}__v2"]
    monkeypatch.setattr(openclaw_control, "task_run_session_keys", lambda task_id: keys)
    monkeypatch.setattr(openclaw_control, "session_statuses", lambda keys: {})
    results = {r["key"]: r["status"] for r in await openclaw_control.abort_task_runs(TID, reason="stop")}
    assert results == {keys[0]: "aborted", keys[1]: "timeout", keys[2]: "error", keys[3]: "no-active-run"}
    calls = [json.loads(line) for line in fake_cli.read_text().splitlines()]
    assert {c[c.index("--params") + 1] for c in calls} == {json.dumps({"key": k, "clearQueued": True}) for k in keys}
    assert all(c[:3] == ["gateway", "call", "sessions.abort"] and "--json" in c for c in calls)
    async with sessions() as db:
        [event] = (await db.execute(select(Event))).scalars().all()
    assert event.event_type == "openclaw.aborted" and event.entity_id == TID
    assert event.event_payload == {"reason": "stop", "sessions": 4, "aborted": [keys[0]],
                                   "unconfirmed": [keys[1], keys[2]], "finished": []}


async def test_sessions_the_gateway_shows_finished_are_not_called(fake_cli, sessions, monkeypatch):
    live, planner, verdict = (f"agent:main:rmp_task_{TID}", f"agent:main:rmp_task_{TID}__plan",
                              f"agent:main:rmp_verify_{TID}__v2")
    monkeypatch.setattr(openclaw_control, "task_run_session_keys", lambda task_id: [live, planner, verdict])
    monkeypatch.setattr(openclaw_control, "session_statuses",
                        lambda keys: {live: "running", planner: "done", verdict: "killed"})
    results = await openclaw_control.abort_task_runs(TID, reason="stop")
    assert [(r["key"], r["status"]) for r in results] == [(live, "aborted")]
    assert len(fake_cli.read_text().splitlines()) == 1
    async with sessions() as db:
        [event] = (await db.execute(select(Event))).scalars().all()
    assert event.event_payload["finished"] == [planner, verdict] and event.event_payload["sessions"] == 3


async def test_a_cli_past_its_timeout_is_killed_with_its_children(tmp_path, monkeypatch):
    marker = tmp_path / "late-abort"
    script = tmp_path / "openclaw"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', \"import time; time.sleep(2); open({str(marker)!r}, 'w')\"])\n"
        "time.sleep(10)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(openclaw_control.shutil, "which", lambda name: str(script))
    monkeypatch.setattr(openclaw_control, "CLI_TIMEOUT_SEC", 1)
    result = await REAL_ABORT(f"agent:main:rmp_task_{TID}")
    await asyncio.sleep(2.5)
    assert result["status"] == "timeout" and not marker.exists()


def test_session_statuses_read_the_gateway_store(tmp_path, monkeypatch):
    import sqlite3

    db = tmp_path / "openclaw-agent.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE session_nodes (session_key TEXT, status TEXT)")
    con.executemany("INSERT INTO session_nodes VALUES (?, ?)", [("a", "running"), ("b", "done"), ("c", None)])
    con.commit()
    con.close()
    monkeypatch.setattr(openclaw_sessions, "AGENT_DB_PATH", db)
    monkeypatch.setattr(openclaw_sessions, "SESSIONS_JSON_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(openclaw_sessions, "_sqlite_sessions_available", lambda db=None: True)
    assert openclaw_sessions.session_statuses(["a", "b", "c", "d"]) == {"a": "running", "b": "done", "c": None}
    assert openclaw_sessions.session_statuses([]) == {}


async def test_a_task_without_sessions_calls_nothing(fake_cli, sessions, monkeypatch):
    monkeypatch.setattr(openclaw_control, "task_run_session_keys", lambda task_id: [])
    assert await openclaw_control.abort_task_runs(TID, reason="stop") == []
    assert not fake_cli.exists()


def test_the_task_run_keys_cover_aura_and_the_evaluator(monkeypatch):
    rows = {
        f"agent:main:rmp_task_{TID}": [(f"agent:main:rmp_task_{TID}", 1), (f"agent:main:rmp_task_{TID}__plan", 2),
                                       (f"agent:main:rmp_task_{TID}__r2", 3), (f"agent:main:rmp_task_{TID}X", 4)],
        f"agent:main:rmp_verify_{TID}": [(f"agent:main:rmp_verify_{TID}", 5), (f"agent:main:rmp_verify_{TID}__v3", 6),
                                         (f"agent:main:rmp_verify_{TID}_fb1", 7)],
    }
    monkeypatch.setattr(openclaw_sessions, "_task_session_rows", lambda prefix: rows.get(prefix, []))
    assert openclaw_sessions.task_run_session_keys(TID) == sorted([
        f"agent:main:rmp_task_{TID}", f"agent:main:rmp_task_{TID}__plan", f"agent:main:rmp_task_{TID}__r2",
        f"agent:main:rmp_verify_{TID}", f"agent:main:rmp_verify_{TID}__v3", f"agent:main:rmp_verify_{TID}_fb1",
    ])


async def test_stop_cancel_rebuild_and_supersede_abort_the_task(monkeypatch, sessions):
    from httpx import ASGITransport, AsyncClient

    from app.api import server
    from app.db.database import get_db

    async def db_override():
        async with sessions() as db:
            yield db

    server.app.dependency_overrides[get_db] = db_override
    scheduled = []
    monkeypatch.setattr(openclaw_control, "schedule_abort", lambda task_id, reason: scheduled.append((task_id, reason)))
    handle = AsyncMock()
    client = AsyncMock()
    client.get_workflow_handle = lambda wid, **kw: handle
    monkeypatch.setattr(server, "connect_temporal", AsyncMock(return_value=client))
    monkeypatch.setattr(temporal_control, "connect_temporal", AsyncMock(return_value=client))
    monkeypatch.setenv("RMP_API_KEY", "k")
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://rmp",
                           headers={"X-RMP-API-Key": "k"}) as api:
        with patch("app.task_registry.messages.add_task_message", AsyncMock(return_value="m1")):
            await api.post(f"/tasks/{TID}/signal", json={"signal_type": "user_input", "message": "stop"})
            await api.post(f"/tasks/{TID}/signal", json={"signal_type": "user_input", "message": "also Kyoto please"})
            await api.post(f"/tasks/{TID}/signal", json={"signal_type": "cancel", "message": ""})
    server.app.dependency_overrides.pop(get_db, None)
    await temporal_control.terminate_task_workflow(TID, "superseded by intake")
    assert scheduled == [(TID, "stop"), (TID, "cancel"), (TID, "superseded by intake")]


async def test_the_background_abort_logs_a_failure_instead_of_raising(monkeypatch, caplog):
    async def boom(task_id, reason):
        raise RuntimeError("store locked")

    monkeypatch.setattr(openclaw_control, "abort_task_runs", boom)
    openclaw_control.schedule_abort(TID, reason="stop")
    await asyncio.sleep(0.05)
    assert "store locked" in caplog.text and not openclaw_control._background
