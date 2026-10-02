import asyncio

import pytest

from app.temporal_control import connect_temporal_with_retry


@pytest.mark.asyncio
async def test_connect_temporal_with_retry_succeeds_after_failures(monkeypatch):
    calls = {"n": 0}

    async def fake_connect(addr, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("not ready")
        return "client"

    async def _no_sleep(_d):
        return None

    monkeypatch.setattr("app.temporal_control.Client.connect", fake_connect)
    monkeypatch.setattr("app.temporal_control.get_temporal_client_kwargs", lambda: {})
    monkeypatch.setattr("app.temporal_control.asyncio.sleep", _no_sleep)
    client = await connect_temporal_with_retry(attempts=5, delay_sec=0.01)
    assert client == "client"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_connect_temporal_with_retry_raises_after_exhaustion(monkeypatch):
    async def fake_connect(addr, **kwargs):
        raise ConnectionError("down")

    async def _no_sleep(_d):
        return None

    monkeypatch.setattr("app.temporal_control.Client.connect", fake_connect)
    monkeypatch.setattr("app.temporal_control.get_temporal_client_kwargs", lambda: {})
    monkeypatch.setattr("app.temporal_control.asyncio.sleep", _no_sleep)
    with pytest.raises(ConnectionError):
        await connect_temporal_with_retry(attempts=3, delay_sec=0.01)


@pytest.mark.asyncio
async def test_intakes_recall_depth_reaches_the_task_workflow(monkeypatch):
    from app import temporal_control

    started = []

    class FakeClient:
        async def start_workflow(self, name, payload, **kwargs):
            started.append(payload)

    async def fake_connect():
        return FakeClient()

    monkeypatch.setattr(temporal_control, "connect_temporal", fake_connect)
    await temporal_control.start_task_workflow("t1", "Good morning!", "s1", "user", recall_depth="none")
    await temporal_control.start_task_workflow("t2", "Good morning!", "s1", "user")
    assert started[0]["recall_depth"] == "none" and "recall_depth" not in started[1]


@pytest.mark.asyncio
async def test_the_workflow_learns_whether_to_recall_and_follow_up(monkeypatch):
    from app import config, temporal_control

    started = []

    class FakeClient:
        async def start_workflow(self, name, payload, **kwargs):
            started.append(payload["deep_recall"])

    async def fake_connect():
        return FakeClient()

    dm = {**config.DEFAULT_DEEP_MEMORY, "recall_enabled": True, "followups_enabled": True, "recall_deadline_sec": 90}
    monkeypatch.setattr(config, "get_deep_memory_config", lambda: dm)
    monkeypatch.setattr(temporal_control, "connect_temporal", fake_connect)
    await temporal_control.start_task_workflow("t1", "Which docs did we use?", "s1", "user", tags=["user-request"])
    await temporal_control.start_task_workflow("t2", "Good morning!", "s1", "user", recall_depth="none")
    await temporal_control.start_task_workflow("t3", "RMP CANARY: Reply with exactly CANARY_OK", "s1", "canary",
                                               tags=["canary"])
    dm["recall_enabled"] = False
    await temporal_control.start_task_workflow("t4", "Which docs did we use?", "s1", "user")
    assert started[0] == {"enabled": True, "followups": True, "deadline_sec": 90, "wait_sec": 300}
    assert [s["enabled"] for s in started] == [True, False, False, False]


@pytest.mark.asyncio
async def test_a_coding_task_starts_the_coding_workflow(monkeypatch):
    from app import temporal_control

    started = []

    class FakeClient:
        async def start_workflow(self, name, payload, **kwargs):
            started.append((name, kwargs["id"]))

    async def fake_connect():
        return FakeClient()

    monkeypatch.setattr(temporal_control, "connect_temporal", fake_connect)
    await temporal_control.start_task_workflow("t1", "Fix the greeting in Aura's code.", "s1", "user", process_type="coding_task")
    await temporal_control.start_task_workflow("t2", "Good morning!", "s1", "user")
    assert started == [("CodingTaskWorkflow", "workflow-t1"), ("GenericTaskWorkflow", "workflow-t2")]


@pytest.mark.asyncio
async def test_terminating_a_task_also_stops_its_coding_units(monkeypatch):
    from app import openclaw_control, temporal_control
    from app.activities import coding_activities

    stopped, terminated = [], []

    class FakeHandle:
        async def signal(self, *args):
            pass

        async def terminate(self, reason):
            terminated.append(reason)

    class FakeClient:
        def get_workflow_handle(self, workflow_id):
            return FakeHandle()

    async def fake_connect():
        return FakeClient()

    monkeypatch.setattr(openclaw_control, "schedule_abort", lambda task_id, reason: None)
    monkeypatch.setattr(coding_activities, "stop_task_units", lambda task_id: stopped.append(task_id) or [])
    monkeypatch.setattr(temporal_control, "connect_temporal", fake_connect)
    assert await temporal_control.terminate_task_workflow("t1", "superseded by intake")
    assert stopped == ["t1"] and terminated == ["superseded by intake"]


@pytest.mark.asyncio
async def test_reconciler_reconnects_after_dead_client(monkeypatch):
    from app import reconciler

    class Dead:
        class service_client:
            @staticmethod
            async def check_health():
                raise ConnectionError("dead")

    class Live:
        class service_client:
            @staticmethod
            async def check_health():
                return True

    monkeypatch.setattr(reconciler, "_temporal_client", Dead())

    async def fake_retry(**kwargs):
        return Live()

    monkeypatch.setattr(
        "app.temporal_control.connect_temporal_with_retry", fake_retry
    )
    client = await reconciler._get_temporal()
    assert client is reconciler._temporal_client
    assert client is not None
