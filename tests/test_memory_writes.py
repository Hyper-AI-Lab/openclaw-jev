"""Tests for episodic write and promotion (provenance kwarg + persistence hooks)."""
import pytest


@pytest.mark.asyncio
async def test_write_episodic_uses_provenance_kwarg(monkeypatch):
    captured = {}

    async def fake_write(*args, **kwargs):
        captured.update(kwargs)
        return "mem-1"

    monkeypatch.setattr(
        "app.memory.router.MemoryRouter.write",
        staticmethod(fake_write),
    )
    from app.activities.db_activities import write_episodic_observation

    mem_id = await write_episodic_observation(
        {
            "process_run_id": "pr-1",
            "task_id": "t-1",
            "step_id": "s-1",
            "text": "Enough observation text for episodic memory write test.",
        }
    )
    assert mem_id == "mem-1"
    assert "provenance" in captured
    assert captured["provenance"]["task_id"] == "t-1"
    assert "provenance_ref" not in captured


@pytest.mark.asyncio
async def test_promotion_uses_provenance_kwarg(monkeypatch):
    from types import SimpleNamespace

    calls, queued = [], []

    async def fake_write(*args, **kwargs):
        calls.append(kwargs)
        return f"mem-{len(calls)}"

    class FakeDb:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, model, key):
            if model.__name__ == "Task":
                return SimpleNamespace(goal="Register the account on example.com", task_type="user")
            return SimpleNamespace(plan_json={"steps": [{"name": "open_site"}, {"name": "register"}]})

    async def enqueue_facts(task_id):
        queued.append(task_id)

    monkeypatch.setattr("app.memory.router.MemoryRouter.write", staticmethod(fake_write))
    monkeypatch.setattr("app.db.database.AsyncSessionLocal", lambda: FakeDb())
    monkeypatch.setattr("app.deep_memory.ingest.enqueue_facts", enqueue_facts)
    monkeypatch.setattr("app.openclaw_sessions.task_action_trace", lambda task_id: [{"tool": "browser_use", "ok": True}])
    from app.memory.promotion import promote_completion_memory

    stats = await promote_completion_memory(
        process_run_id="pr-1",
        process_type="account_registration",
        task_id="t-1",
        episodic_content="Registration complete at https://example.com/register.",
    )
    assert stats == {"facts_queued": 1, "promoted_procedural": 1, "rejected": 0} and queued == ["t-1"]
    assert [c["scope_type"] for c in calls] == ["procedural"]
    assert all("provenance" in c for c in calls)
    assert all("provenance_ref" not in c for c in calls)


@pytest.mark.asyncio
async def test_build_context_fail_soft(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.deep_memory.curator.assemble_fast_context", boom)
    from app.activities.db_activities import build_process_memory_context

    block = await build_process_memory_context({"process_run_id": "pr-1"})
    assert block == ""


@pytest.mark.asyncio
async def test_read_memory_fail_soft(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "app.memory.router.MemoryRouter.read_ordered",
        staticmethod(boom),
    )
    from app.activities.db_activities import read_process_memory

    items = await read_process_memory({"ordered": True, "scope_id": "pr-1"})
    assert items == []
