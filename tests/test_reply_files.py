"""Files Aura sends Kirill with her reply: only from the task's Claude sessions, checked twice, sent once."""
import dataclasses
import hashlib
import json
import urllib.parse
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment

from app.activities import openclaw_activities as oa
from app.activities import side_effects
from app.api import server
from app.coding import direct, outbox
from app.db.models import Base, Event, SideEffectReceipt, Task, TaskMessage

TASK = "11111111-2222-4333-8444-555555555555"
OTHER = "66666666-7777-4888-9999-000000000000"
SLACK_KEY = "agent:main:slack:channel:d0test"
REAL_CLIENT = httpx.AsyncClient
SLACK = {
    "conversations.open": (200, {"ok": True, "channel": {"id": "D1"}}),
    "files.getUploadURLExternal": (200, {"ok": True, "upload_url": "https://files.slack.com/upload/v1/abc", "file_id": "F1"}),
    "upload": (200, "OK - 8"),
    "files.completeUploadExternal": (200, {"ok": True, "files": [{"id": "F1"}]}),
    "chat.postMessage": (200, {"ok": True, "ts": "1790.0042"}),
}


class FakeSlack:
    """Slack's Web API and its upload host, answering by method."""

    def __init__(self, **answers):
        self.answers = {**SLACK, **answers}
        self.calls = []

    def client(self, *args, **kwargs):
        def handler(request):
            name = request.url.path.rsplit("/", 1)[-1] if request.url.host == "slack.com" else "upload"
            self.calls.append((name, request.content.decode(errors="replace")))
            status, body = self.answers[name]
            return httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)

        return REAL_CLIENT(transport=httpx.MockTransport(handler))


def made(task_id, name, data, session="s1"):
    path = direct.DIRECT_DIR / task_id / session / "scratch" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
async def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'files.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all,
                            tables=[m.__table__ for m in (Task, Event, SideEffectReceipt, TaskMessage)])
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(side_effects, "AsyncSessionLocal", factory)
    async with factory() as s:
        s.add_all([Task(id=TASK, goal="Convert the sheet", task_type="user", status="running"),
                   Task(id=OTHER, goal="Old work", task_type="user", status="completed")])
        await s.commit()
    yield factory
    await engine.dispose()


async def attach(factory, path, title=""):
    payload = {"id": str(uuid.uuid4()), "title": title or path.name, **outbox.check(TASK, str(path))}
    async with factory() as s:
        s.add(Event(correlation_id=TASK, entity_type="task", entity_id=TASK, event_type="reply.file_attached",
                    event_payload=payload))
        await s.commit()
    return payload


def test_only_a_file_from_the_tasks_claude_sessions_goes_and_never_a_secret(tmp_path, monkeypatch):
    good = made(TASK, "out.csv", b"a,b\n1,2\n")
    assert outbox.check(TASK, str(good)) == {"name": "out.csv", "path": str(good.resolve()), "size": 8,
                                             "sha256": hashlib.sha256(b"a,b\n1,2\n").hexdigest()}
    assert outbox.check(TASK, str(made(TASK, "chart.png", b"\x89PNG\r\n\x1a\n\xff\xfe")))["size"] == 10
    outbound = outbox.OUTBOUND_DIR / "report.pdf"
    outbound.parent.mkdir(parents=True, exist_ok=True)
    outbound.write_bytes(b"%PDF-1.7\n")
    assert outbox.check(TASK, str(outbound))["name"] == "report.pdf"
    elsewhere = tmp_path / "elsewhere.csv"
    elsewhere.write_text("x\n")
    link = good.parent / "link.csv"
    link.symlink_to(elsewhere)
    refusals = {
        str(elsewhere): "this task's Claude sessions",
        str(link): "this task's Claude sessions",
        str(made(OTHER, "theirs.csv", b"x\n")): "this task's Claude sessions",
        str(good.parent): "not a regular file",
        str(made(TASK, "empty.txt", b"")): "1 to",
        str(made(TASK, "keys.env", b"OPENAI_API_KEY=sk-" + b"a" * 30 + b"\n")): "looks like it holds a secret",
        str(good.parent / "missing.csv"): "does not exist",
    }
    for path, words in refusals.items():
        with pytest.raises(outbox.FileRefused, match=words):
            outbox.check(TASK, path)
    monkeypatch.setattr(outbox, "MAX_BYTES", 4)
    with pytest.raises(outbox.FileRefused, match="1 to 4 bytes"):
        outbox.check(TASK, str(good))


async def test_aura_attaches_files_only_while_her_task_runs_and_ten_at_most(db, monkeypatch):
    async def get_db():
        async with db() as s:
            yield s

    server.app.dependency_overrides[server.get_db] = get_db
    monkeypatch.setenv("RMP_API_KEY", "k")
    out = made(TASK, "out.csv", b"a,b\n1,2\n")
    mine = f"agent:main:rmp_task_{TASK}"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://rmp",
                                     headers={"X-RMP-API-Key": "k"}) as api:
            attached = (await api.post("/api/replies/files",
                                       json={"session_key": mine, "path": str(out), "title": "The sheet as CSV"})).json()
            finished = await api.post("/api/replies/files", json={"session_key": f"agent:main:rmp_task_{OTHER}",
                                                                  "path": str(made(OTHER, "x.csv", b"x\n"))})
            outside = await api.post("/api/replies/files", json={"session_key": mine, "path": "/etc/hostname"})
            monkeypatch.setattr(outbox, "MAX_FILES", 1)
            full = await api.post("/api/replies/files", json={"session_key": mine, "path": str(out)})
    finally:
        server.app.dependency_overrides.pop(server.get_db, None)
    assert attached["name"] == "out.csv" and attached["title"] == "The sheet as CSV" and attached["size"] == 8
    assert finished.status_code == 409 and outside.status_code == 409
    assert "this task's Claude sessions" in outside.json()["detail"]
    assert full.status_code == 409 and "already wait" in full.json()["detail"]
    assert [f["id"] for f in await side_effects.pending_reply_files(TASK)] == [attached["id"]]


async def test_files_go_to_kirill_once_each_and_one_changed_since_it_was_attached_does_not(db, monkeypatch):
    await attach(db, made(TASK, "out.csv", b"a,b\n1,2\n"), "The sheet as CSV")
    notes = made(TASK, "notes.txt", b"first\n")
    await attach(db, notes)
    notes.write_bytes(b"changed\n")
    slack = FakeSlack()
    monkeypatch.setattr(side_effects.httpx, "AsyncClient", slack.client)
    assert await side_effects.send_reply_files(TASK, "U1", "xoxb-test") == ["out.csv"]
    assert [name for name, _ in slack.calls] == ["conversations.open", "files.getUploadURLExternal", "upload",
                                                 "files.completeUploadExternal", "chat.postMessage"]
    shared = dict(urllib.parse.parse_qsl(slack.calls[3][1]))
    assert shared["channel_id"] == "D1" and json.loads(shared["files"]) == [{"id": "F1", "title": "The sheet as CSV"}]
    assert "RMP did not send notes.txt, a file Aura attached: notes.txt changed after Aura attached it" in slack.calls[4][1]
    assert await side_effects.pending_reply_files(TASK) == []
    assert await side_effects.send_reply_files(TASK, "U1", "xoxb-test") == [] and len(slack.calls) == 5
    async with db() as s:
        kinds = (await s.execute(select(Event.event_type).where(Event.entity_id == TASK))).scalars().all()
    assert "reply.file_sent" in kinds and "reply.file_refused" in kinds


async def test_a_slack_outage_raises_for_a_retry_and_an_upload_slack_refuses_is_told(db, monkeypatch):
    await attach(db, made(TASK, "out.csv", b"a,b\n"))
    down = FakeSlack(**{"files.getUploadURLExternal": (503, {"ok": False})})
    monkeypatch.setattr(side_effects.httpx, "AsyncClient", down.client)
    with pytest.raises(side_effects.SlackTransientError):
        await side_effects.send_reply_files(TASK, "U1", "xoxb-test")
    assert len(await side_effects.pending_reply_files(TASK)) == 1
    refusing = FakeSlack(**{"files.completeUploadExternal": (200, {"ok": False, "error": "invalid_channel"})})
    monkeypatch.setattr(side_effects.httpx, "AsyncClient", refusing.client)
    assert await side_effects.send_reply_files(TASK, "U1", "xoxb-test") == []
    assert "files.completeUploadExternal: invalid_channel" in refusing.calls[-1][1]
    assert await side_effects.pending_reply_files(TASK) == []


@pytest.fixture
def slack_config(monkeypatch):
    monkeypatch.setattr(oa, "should_suspend_slack", lambda: False)
    monkeypatch.setattr(oa, "get_slack_bot_token", lambda: "xoxb-test")
    monkeypatch.setattr(oa, "_get_slack_user_id", lambda key: "U1")


async def test_a_delivered_reply_with_files_waiting_says_so(slack_config, monkeypatch):
    monkeypatch.setattr(side_effects, "send_slack_message_idempotent", AsyncMock(return_value=True))
    waiting = []
    monkeypatch.setattr(side_effects, "pending_reply_files", AsyncMock(side_effect=lambda task_id: list(waiting)))
    payload = {"task_id": TASK, "session_key": SLACK_KEY, "intent": "Convert the sheet to CSV", "task_type": "user",
               "message": "Here is the sheet as CSV."}
    env = ActivityEnvironment()
    assert await env.run(oa.notify_slack_user, {**payload, "message_kind": "reply"}) == oa.SLACK_DELIVERED
    waiting.append({"id": "f1", "name": "out.csv"})
    assert await env.run(oa.notify_slack_user, {**payload, "message_kind": "reply"}) == oa.SLACK_DELIVERED_WITH_FILES
    assert await env.run(oa.notify_slack_user, {**payload, "message_kind": "notice"}) == oa.SLACK_DELIVERED


async def test_the_last_try_tells_kirill_which_files_could_not_go(slack_config, monkeypatch):
    outage = side_effects.SlackTransientError("files.getUploadURLExternal failed for now: http 503")
    monkeypatch.setattr(side_effects, "send_reply_files", AsyncMock(side_effect=outage))
    monkeypatch.setattr(side_effects, "pending_reply_files", AsyncMock(return_value=[{"id": "f1", "name": "out.csv"}]))
    told = AsyncMock(return_value=True)
    monkeypatch.setattr(side_effects, "send_slack_message_idempotent", told)
    env = ActivityEnvironment()
    with pytest.raises(side_effects.SlackTransientError):
        await env.run(oa.deliver_reply_files, {"task_id": TASK, "session_key": SLACK_KEY})
    env.info = dataclasses.replace(env.info, attempt=oa.REPLY_FILE_ATTEMPTS)
    assert await env.run(oa.deliver_reply_files, {"task_id": TASK, "session_key": SLACK_KEY}) == []
    assert "RMP could not send out.csv, attached by Aura, after several tries" in told.await_args.args[2]
