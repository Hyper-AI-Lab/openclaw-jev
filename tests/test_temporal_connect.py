"""The process's one Temporal client (specs/WP-04.md WP04-20, WP04-21), and how task workflows are started and
ended through it."""
import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config, temporal_control

REPO = Path(__file__).resolve().parents[1]


class FakeClient:
    """A connected client whose health check passes until ``healthy`` is cleared."""

    def __init__(self):
        self.healthy = True
        self.service_client = SimpleNamespace(check_health=self._check_health)

    async def _check_health(self):
        if not self.healthy:
            raise ConnectionError("Temporal went away")
        return True


@pytest.fixture
def connects(monkeypatch):
    """Client.connect replaced, and no client cached yet. Each connect is recorded as (address, keyword arguments)
    and returns a new FakeClient, unless an exception is queued in ``failures``; the sleeps between attempts are
    recorded, not slept."""
    record = SimpleNamespace(calls=[], failures=[], slept=[])

    async def connect(address, **kwargs):
        record.calls.append((address, kwargs))
        # A real connect yields to the loop: concurrent callers meet here.
        await asyncio.sleep(0)
        if record.failures:
            raise record.failures.pop(0)
        return FakeClient()

    async def sleep(seconds):
        record.slept.append(seconds)

    monkeypatch.setattr(temporal_control, "_clients", {})
    monkeypatch.setattr(temporal_control.Client, "connect", connect)
    monkeypatch.setattr(temporal_control, "get_temporal_client_kwargs", lambda: {})
    # Only temporal_control's back-off sleeps are recorded; the fake connect above still yields.
    monkeypatch.setattr(temporal_control, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep}))
    return record


async def test_one_client_per_loop(connects):
    first, second = await asyncio.gather(temporal_control.connect_temporal(), temporal_control.connect_temporal())
    assert first is second
    assert await temporal_control.connect_temporal() is first
    assert connects.calls == [(config.TEMPORAL_ADDRESS, {})]

    # Another event loop gets its own client; once that loop is closed, its entry is dropped.
    other = await asyncio.to_thread(asyncio.run, temporal_control.connect_temporal())
    assert other is not first and len(connects.calls) == 2
    assert await temporal_control.connect_temporal() is first
    assert [cached for _, cached, _ in temporal_control._clients.values()] == [first]


async def test_a_closed_loop_purged_by_another_thread_first_is_no_error(connects, monkeypatch):
    """Every loop's thread purges closed loops from the one dict. When another thread drops the same entry between this
    call's look and its purge, this call still gets its client."""
    closed = asyncio.new_event_loop()
    closed.close()

    class PurgedMeanwhile(dict):
        def items(self):
            seen = list(super().items())
            self.pop(id(closed), None)  # the other thread's purge
            return seen

    clients = PurgedMeanwhile({id(closed): (closed, FakeClient(), asyncio.Lock())})
    monkeypatch.setattr(temporal_control, "_clients", clients)

    client = await temporal_control.connect_temporal()

    assert len(connects.calls) == 1
    assert [(owner, cached) for owner, cached, _ in clients.values()] == [(asyncio.get_running_loop(), client)]


async def test_a_failed_health_check_reconnects(connects, caplog):
    caplog.set_level(logging.WARNING, logger="rmp.temporal_control")
    first = await temporal_control.connect_temporal()
    first.healthy = False

    second = await temporal_control.connect_temporal()

    assert second is not first and len(connects.calls) == 2
    assert await temporal_control.connect_temporal() is second and len(connects.calls) == 2
    assert [r.getMessage() for r in caplog.records] == [
        "Temporal client failed its health check; reconnecting: Temporal went away"]


async def test_attempts_back_off_then_raise(connects, caplog):
    caplog.set_level(logging.WARNING, logger="rmp.temporal_control")
    connects.failures.extend([ConnectionError("not ready")] * 2)

    client = await temporal_control.connect_temporal(attempts=5, delay_sec=1.0)

    assert len(connects.calls) == 3 and connects.slept == [1.0, 2.0]
    client.healthy = False
    connects.failures.extend([ConnectionError("down")] * 6)
    connects.slept.clear()
    caplog.clear()
    with pytest.raises(ConnectionError, match="down"):
        await temporal_control.connect_temporal(attempts=6, delay_sec=1.0)
    # The delay doubles up to 8 s; each failed try is one WARNING.
    assert connects.slept == [1.0, 2.0, 4.0, 8.0, 8.0]
    assert [r.getMessage() for r in caplog.records] == [
        "Temporal client failed its health check; reconnecting: Temporal went away",
        *[f"Temporal connect {i}/6 failed: down" for i in range(1, 7)]]
    # Nothing is cached after a failed reconnect: the next call connects again.
    assert [cached for _, cached, _ in temporal_control._clients.values()] == [None]
    assert await temporal_control.connect_temporal() is not client and len(connects.calls) == 10


async def test_the_interceptor_is_passed(connects, monkeypatch):
    """With telemetry ready (the API and the worker), every client gets the tracing interceptor; readiness's check
    connected without it before."""
    from temporalio.contrib.opentelemetry import TracingInterceptor

    from app import telemetry
    from app.production import readiness

    monkeypatch.setattr(temporal_control, "get_temporal_client_kwargs", telemetry.get_temporal_client_kwargs)
    monkeypatch.setattr(telemetry, "is_telemetry_enabled", lambda: True)
    monkeypatch.setitem(telemetry._status, "ready", True)
    monkeypatch.setitem(telemetry._status, "temporal_tracing", False)

    result = await readiness.check_temporal()

    assert result.status == "pass"
    [(address, kwargs)] = connects.calls
    assert address == config.TEMPORAL_ADDRESS
    assert [type(interceptor) for interceptor in kwargs["interceptors"]] == [TracingInterceptor]


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


async def test_the_reconciler_uses_the_shared_client_with_its_retries(monkeypatch):
    from app import reconciler

    asked = []

    async def connect(**kwargs):
        asked.append(kwargs)
        raise ConnectionError("down")

    monkeypatch.setattr(reconciler, "connect_temporal", connect)
    assert await reconciler.count_stuck_running_workflows() == -1
    assert asked == [{"attempts": 4, "delay_sec": 0.5}]


def test_the_address_has_one_default(monkeypatch):
    """The watchdog's probe cannot import app, so it repeats app.config's default; the two stay equal."""
    probe = ast.parse((REPO / "ops" / "temporal_healthcheck.py").read_text())
    [default] = [node.args[1].value for node in ast.walk(probe)
                 if isinstance(node, ast.Call) and len(node.args) == 2
                 and isinstance(node.args[0], ast.Constant) and node.args[0].value == "TEMPORAL_ADDRESS"]
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    assert config._temporal_address() == default


def _guarded_files():
    """Every source file of app/, ops/ and worker.py; compiled caches are no source."""
    files = [path for root in ("app", "ops") for path in (REPO / root).rglob("*")
             if path.is_file() and "__pycache__" not in path.parts]
    return sorted([*files, REPO / "worker.py"])


# The only 7233s left: the configured default and the probe's copy of it, and two port lists that are not addresses.
PORT_7233 = (
    ("app/config.py", '"TEMPORAL_ADDRESS", "localhost:7233"'),
    ("app/config.py", '"7233-7243"'),  # the coder firewall's blocked_tcp_ports
    ("ops/temporal_healthcheck.py", '"TEMPORAL_ADDRESS", "localhost:7233"'),
    ("ops/claude_code_smoke.py", "6333, 7233, 8000"),  # the smoke test's port list
)


def test_no_temporal_address_literal():
    hits = []
    for path in _guarded_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = str(path.relative_to(REPO))
        hits += [(rel, line.strip()) for line in text.splitlines() if "7233" in line]
    unexplained = [hit for hit in hits if not any(hit[0] == rel and part in hit[1] for rel, part in PORT_7233)]
    assert unexplained == [], "a Temporal address outside app.config:\n" + "\n".join(map(str, unexplained))
    assert len(hits) == len(PORT_7233)


def _client_connects(tree):
    """The lines that name Client.connect: on ``Client``, on a name an import binds to temporalio.client's Client,
    or at the end of an attribute chain such as ``temporalio.client.Client.connect``."""
    names = {"Client"} | {alias.asname for node in ast.walk(tree)
                          if isinstance(node, ast.ImportFrom) and node.module == "temporalio.client"
                          for alias in node.names if alias.name == "Client" and alias.asname}
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "connect"
            and ((isinstance(node.value, ast.Name) and node.value.id in names)
                 or (isinstance(node.value, ast.Attribute) and node.value.attr == "Client"))]


@pytest.mark.parametrize("source", [
    "import temporalio.client\n\nasync def client():\n    return await temporalio.client.Client.connect(address)\n",
    "from temporalio.client import Client as Temporal\n\nasync def client():\n    return await Temporal.connect(address)\n",
], ids=["attribute chain", "import alias"])
def test_the_guard_sees_client_connect_by_any_name(source):
    assert _client_connects(ast.parse(source)) == [4]


def test_client_connect_appears_twice():
    """Every Temporal client comes from connect_temporal; the watchdog's probe, which cannot import app, is the one
    other Client.connect."""
    found = []
    for path in _guarded_files():
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue  # ops/migrate_task_registry_schema.py is bash
        found += [str(path.relative_to(REPO))] * len(_client_connects(tree))
    assert found == ["app/temporal_control.py", "ops/temporal_healthcheck.py"]
