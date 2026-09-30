"""The whole Slack path in one process: POST /tasks, intake, Temporal, evaluator, Slack.

Everything on the path runs for real on a fresh SQLite file: the API, intake handling, the
task and intake workflows, every DB and delivery activity, the evaluator's prompt, parsing
and verdict records, and Slack splitting, idempotency and ledger. Only the boundaries are
stubbed: the intake analyst's answer, Aura, the evaluator's model turn, the planner, vector
search and OpenClaw transcripts. The network is sealed: Slack answers from a fake, RMP's own
POST /tasks is this app, and any other way out of the process fails the test.
"""
import asyncio
import hashlib
import json
import socket
import sys
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app import config, openclaw_sessions, temporal_control
from app.activities import db_activities as db, openclaw_activities as oc, plan_activities
from app.activities import deep_memory_activities as dm
from app.activities.intake_activities import resubmit_user_messages
from app.activities.side_effects import SLACK_PART_CHARS
from app.api import server
from app.db import database
from app.db.models import (
    Base, DeepContextReport, Event, MemoryItem, ProcessRun, SideEffectReceipt, Task, TaskIntakeDecision, TaskMessage,
)
from app.deep_memory import curator, index as deep_index, recall
from app.memory import router
from app.orchestrator import web_capability
from app.orchestrator.completion_rework import RECALL_NOTICE
from app.production import invariants
from app.task_registry import intake_runner
from app.task_registry.intake_decision_engine import apply_intake_policy
from app.task_registry.retriever import fetch_active_tasks
from app.workflows.generic_execute_child import GenericExecuteChildWorkflow
from app.workflows.generic_task import GenericTaskWorkflow
from app.workflows.intake_workflow import IntakeWorkflow

SESSION = "agent:main:slack:channel:d0test"
KEY = "whole-path-key"
BOUND = 60
FACTS = '\n\n```json\n{"facts": {"step_complete": true}}\n```'
PLAN = {"steps": [{"name": "answer", "kind": "deliver", "predicate_id": "deliver", "prompt": "Answer the user."}]}
ACCEPT = {"verdict": "accept", "quality": "pass", "reason": "Answers the ask."}
# worker.py's activities, less the two stubbed ones: Aura (send_to_openclaw) and the intake analyst.
WORKER_ACTIVITIES = [
    oc.validate_openclaw_output, oc.parse_agent_evaluation, db.update_task_status, oc.notify_slack_user,
    oc.task_actions_digest,
    oc.check_intermediate_updates_enabled, oc.verify_response_quality, db.ensure_process_run,
    db.acquire_process_run_lease, db.release_process_run_lease, db.finalize_task_failure,
    db.execute_compensation, db.update_process_state, db.record_step, db.record_observation, db.record_event,
    db.write_process_memory, db.read_process_memory, db.build_process_memory_context,
    db.write_episodic_observation, db.compact_episodic_memory, db.promote_completion_memory,
    db.register_artifact, db.list_process_artifacts, plan_activities.generate_process_plan,
    plan_activities.save_process_plan, resubmit_user_messages,
]


def rework(command):
    return {"verdict": "rework", "quality": "fail", "issues": command, "command_to_aura": command}


@workflow.defn(name="DeepRecallWorkflow", sandboxed=False)
class ScriptedRecall:
    """The recall child with its real report row and a scripted search (the models are boundaries)."""

    @workflow.run
    async def run(self, payload):
        report_id = await workflow.execute_activity(
            dm.start_recall_report, payload, start_to_close_timeout=timedelta(seconds=15))
        await workflow.execute_activity("recall_script", payload, start_to_close_timeout=timedelta(seconds=30))
        return await workflow.execute_activity(
            dm.read_recall_step, {"report_id": report_id, "query": payload["query"], "dialogue": "", "evidence": []},
            start_to_close_timeout=timedelta(seconds=30))


class Harness:
    """Scripts the stubbed boundaries and records what crossed them."""

    def __init__(self, env, api, sessions):
        self.env, self.api, self.sessions = env, api, sessions
        self.asgi = httpx.ASGITransport(app=server.app)
        self.intake, self.drafts, self.verdicts, self.during = [], [], [], {}
        self.prompts, self.plans, self.judged = [], [], []
        self.sessions_used, self.verdict_sessions = [], []
        self.bare = set()  # dispatch numbers whose draft comes back without its facts block
        self.slack, self.channels, self.blocked = [], set(), []
        self.slack_error = None
        self.while_posting = None
        self.recall_released = asyncio.Event()

    async def send(self, text, slack_ts):
        """A Slack DM as the rmp_adapter plugin posts it."""
        body = {"intent": text, "raw_text": text, "tags": ["user-request"], "user_id": "slack_user",
                "session_key": SESSION, "slack_message_id": slack_ts,
                "idempotency_key": hashlib.sha256(f"{SESSION}:msg:{slack_ts}".encode()).hexdigest()}
        resp = await asyncio.wait_for(self.api.post("/tasks", json=body), BOUND)
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def stop(self):
        """A whole-message stop as the plugin routes it: straight to the session's active task."""
        active = (await self.api.get(f"/sessions/{SESSION}/active_user_task")).json()["active_task"]
        await self.api.post(f"/tasks/{active['id']}/signal", json={"signal_type": "user_input", "message": "stop"})

    async def finish(self, task_id):
        return await asyncio.wait_for(self.env.client.get_workflow_handle(f"workflow-{task_id}").result(), BOUND)

    async def rows(self, model, *where, order=None):
        async with self.sessions() as s:
            return list((await s.execute(select(model).where(*where).order_by(order))).scalars())

    async def events(self, task_id):
        return [e.event_type for e in await self.rows(Event, Event.entity_id == task_id, order=Event.occurred_at)]

    async def said(self, task_id, role):
        messages = await self.rows(TaskMessage, TaskMessage.task_id == task_id, TaskMessage.role == role,
                                   order=TaskMessage.created_at)
        return [m.content for m in messages]

    async def task(self, task_id):
        return (await self.rows(Task, Task.id == task_id))[0]

    def activities(self):
        h = self

        @activity.defn(name="classify_task_intake")
        async def classify_task_intake(payload):
            """The analyst's answer is scripted; the active tasks it sees and the policy it answers to are real."""
            context = {**payload, "active_tasks": await fetch_active_tasks(session_key=payload["session_key"])}
            answer = {"confidence": 90, "execution_mode": "conversational", **h.intake.pop(0)}
            return {**apply_intake_policy(answer, context, tags=payload["tags"]), "request_hash": payload["intent"][:40]}

        @activity.defn(name="send_to_openclaw")
        async def send_to_openclaw(payload):
            h.prompts.append(payload["message"])
            h.sessions_used.append(payload.get("session_suffix") or "")
            if len(h.prompts) in h.during:
                await h.during.pop(len(h.prompts))(payload)
            draft = h.drafts.pop(0) if h.drafts else "An Aura turn that no test scripted."
            return {"result": {"payloads": [{"text": draft if len(h.prompts) in h.bare else draft + FACTS}]}}

        @activity.defn(name="recall_script")
        async def recall_script(payload):
            """The recall's search, finishing once the test lets it."""
            await h.recall_released.wait()

        return [classify_task_intake, send_to_openclaw, recall_script, dm.start_recall_report, dm.read_recall_step,
                dm.judge_recall_novelty, dm.settle_recall_report, *WORKER_ACTIVITIES]

    async def evaluator_turn(self, task_id, prompt, verdict=0):
        self.judged.append(prompt)
        self.verdict_sessions.append(verdict)
        return json.dumps(self.verdicts.pop(0) if self.verdicts else ACCEPT)

    async def planner(self, payload):
        self.plans.append(payload["message"])
        return {"result": {"payloads": [{"text": json.dumps(PLAN)}]}}

    def refuse(self, what):
        self.blocked.append(what)
        return f"hermetic test: {what} is off limits"

    async def internet(self, request):
        url = request.url
        if url.host == "slack.com":
            body = json.loads(request.content)
            self.slack.append(body["text"])
            self.channels.add(body["channel"])
            if self.while_posting:
                hook, self.while_posting = self.while_posting, None
                await hook()
            if self.slack_error:
                return httpx.Response(200, json={"ok": False, "error": self.slack_error})
            return httpx.Response(200, json={"ok": True, "ts": f"1790000000.{len(self.slack):06d}"})
        if (url.host, url.port) == ("127.0.0.1", 8000):
            if (request.method, url.path) == ("POST", "/tasks"):
                return await self.asgi.handle_async_request(request)
            if url.path in ("/health", "/api/production/readiness"):
                return httpx.Response(200, json={"status": "ok"})
        raise httpx.ConnectError(self.refuse(f"{request.method} {url}"), request=request)

    def install(self, monkeypatch):
        h, real_connect, real_temporal = self, socket.socket.connect, Client.connect

        async def test_temporal():
            return h.env.client

        async def route(transport, request):
            return await h.internet(request)

        def sync_http(transport, request):
            raise httpx.ConnectError(h.refuse(f"{request.method} {request.url}"), request=request)

        def connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                raise ConnectionRefusedError(h.refuse(f"socket {address}"))
            return real_connect(sock, address)

        async def temporal(target_host, *args, **kwargs):
            if target_host.endswith(":7233"):
                raise RuntimeError(h.refuse(f"Temporal at {target_host}"))
            return await real_temporal(target_host, *args, **kwargs)

        for target, name, value in (
            (httpx.AsyncHTTPTransport, "handle_async_request", route),
            (httpx.HTTPTransport, "handle_request", sync_http),
            (socket.socket, "connect", connect),
            (Client, "connect", staticmethod(temporal)),
            (server, "connect_temporal", test_temporal),
            (temporal_control, "connect_temporal", test_temporal),
            (intake_runner, "connect_temporal", test_temporal),
            (oc, "_execute_on_internal_session", h.evaluator_turn),
            (oc, "get_slack_bot_token", lambda: "xoxb-test"),
            (plan_activities, "send_to_openclaw", h.planner),
            (openclaw_sessions, "task_action_trace", lambda *args, **kwargs: []),
            (router, "get_vector_service", lambda: SimpleNamespace(search=lambda *args: [])),
            (deep_index, "collection_exists", lambda: True),
            (deep_index, "search", lambda *args, **kwargs: []),
            (web_capability, "obscura_available", lambda: False),
        ):
            monkeypatch.setattr(target, name, value)


def session_users():
    return [m for n, m in list(sys.modules.items()) if n.startswith("app.") and getattr(m, "AsyncSessionLocal", None)]


@pytest.fixture
async def h(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rmp.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    default = database.AsyncSessionLocal
    for module in session_users():
        monkeypatch.setattr(module, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(config, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("RMP_API_KEY", KEY)
    monkeypatch.setenv("AURA_JEV_MODE", "off")
    async with await WorkflowEnvironment.start_time_skipping() as env, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://rmp", headers={"X-RMP-API-Key": KEY}
    ) as api:
        harness = Harness(env, api, sessions)
        harness.install(monkeypatch)
        async with Worker(env.client, task_queue="openclaw-tasks", activities=harness.activities(),
                          workflows=[GenericTaskWorkflow, GenericExecuteChildWorkflow, IntakeWorkflow,
                                     ScriptedRecall]):
            yield harness
    for module in session_users():
        if module.AsyncSessionLocal is sessions:  # first imported mid-test, so monkeypatch cannot undo it
            module.AsyncSessionLocal = default
    await engine.dispose()
    assert harness.blocked == []


async def invariants_once_settled(monkeypatch):
    monkeypatch.setattr(invariants, "SETTLE_MINUTES", 0)
    checks = (invariants.check_judged_deliveries, invariants.check_attached_messages, invariants.check_slack_delivery)
    return {result.name: result for result in [await check() for check in checks]}


async def test_a_new_request_is_reworked_and_only_the_accepted_draft_reaches_slack(h, monkeypatch):
    ask = "What should I pack for Osaka in October?"
    first = "Pack light layers and a rain jacket."
    second = "Pack light layers, a rain jacket and comfortable walking shoes."
    h.intake = [{"decision": "create_fresh", "execution_mode": "structured_work"}]
    h.drafts, h.verdicts = [first, second], [rework("Say which shoes to bring."), ACCEPT]

    tid = (await h.send(ask, "1790000001.000100"))["task_id"]
    result = await h.finish(tid)

    assert result == {"status": "completed", "task_id": tid, "final_result": second}
    assert h.slack == [second] and h.channels == {config.get_slack_owner_user_id()}
    assert len(h.plans) == 1 and ask in h.prompts[0] and "Say which shoes to bring." in h.prompts[1]
    assert len(h.judged) == 2 and first in h.judged[0] and second in h.judged[1]
    events = Counter(await h.events(tid))
    assert (events["evaluator.verdict"], events["evaluator.accept"], events["slack.delivered"]) == (2, 1, 1)
    assert (await h.task(tid)).status == "completed"
    assert (await h.rows(ProcessRun, ProcessRun.task_id == tid))[0].plan_json["source"] == "plan_llm"
    assert await h.said(tid, "user") == [ask] and await h.said(tid, "assistant") == [second]
    checks = await invariants_once_settled(monkeypatch)
    assert {name: c.status for name, c in checks.items()} == dict.fromkeys(checks, "pass")
    # The first draft came from the task's session, the rework from a fresh one; each verdict had its own.
    assert h.sessions_used == ["", "__r2"] and h.verdict_sessions == [1, 2]
    assert h.prompts[1].count(curator.HEADER) == 1 and "ACTIONS ALREADY TAKEN IN THIS TASK" in h.prompts[1]


async def test_a_follow_up_sent_while_aura_works_is_folded_into_the_one_judged_reply(h, monkeypatch):
    ask, follow_up = "Plan a three-day food itinerary for Osaka.", "Please include a vegetarian option each day."
    first = "Day one Dotonbori, day two Kuromon Market, day three Shinsekai."
    folded = "Day one Dotonbori, day two Kuromon Market, day three Shinsekai, each with a vegetarian stall."
    attached = {}

    async def follow_up_arrives(payload):
        h.intake.append({"decision": "attach_active", "target_task_id": payload["task_id"]})
        attached.update(await h.send(follow_up, "1790000002.000200"))

    h.intake, h.drafts, h.during = [{"decision": "create_fresh"}], [first, folded], {1: follow_up_arrives}
    tid = (await h.send(ask, "1790000002.000100"))["task_id"]
    result = await h.finish(tid)

    assert attached["intake_action"] == "attach_active" and attached["attached_task_ids"] == [tid]
    assert len(h.prompts) == 2 and follow_up in h.prompts[1] and first in h.prompts[1]
    assert result["status"] == "completed" and result["final_result"] == folded
    assert h.slack == [f"Got it: adding \u201c{follow_up}\u201d to the task I'm working on ({tid[:8]}).", folded]
    kinds = await h.events(tid)
    assert kinds.index("intake.attach") < kinds.index("evaluator.accept")
    assert Counter(kinds)["evaluator.verdict"] == 1 and Counter(kinds)["slack.delivered"] == 2
    assert await h.said(tid, "user") == [ask, follow_up] and await h.said(tid, "assistant") == h.slack
    checks = await invariants_once_settled(monkeypatch)
    assert {name: c.status for name, c in checks.items()} == dict.fromkeys(checks, "pass")


async def test_the_conversation_log_records_each_messages_kind_session_and_place(h):
    ask, follow_up = "Plan a day in Kobe.", "Add a place for Kobe beef at lunch."
    first, folded = "Harborland in the morning, Kitano in the afternoon.", "Harborland, Kobe beef at Mouriya, Kitano."

    async def follow_up_arrives(payload):
        h.intake.append({"decision": "attach_active", "target_task_id": payload["task_id"]})
        await h.send(follow_up, "1790000011.000200")

    h.intake, h.drafts, h.during = [{"decision": "create_fresh"}], [first, folded], {1: follow_up_arrives}
    tid = (await h.send(ask, "1790000011.000100"))["task_id"]
    await h.finish(tid)

    log = await h.rows(TaskMessage, TaskMessage.task_id == tid, order=TaskMessage.created_at)
    assert [(m.role, m.kind) for m in log] == [
        ("user", "request"), ("user", "attached"), ("assistant", "notice"),
        ("evaluator", "verdict"), ("assistant", "reply"),
    ]
    assert {m.session_key for m in log} == {SESSION}
    request, attached, _notice, verdict, reply = log
    assert request.meta["slack"]["message_id"] == "1790000011.000100" and request.meta["task_type"] == "user"
    assert attached.meta["slack"]["message_id"] == "1790000011.000200" and attached.meta["intake_decision_id"]
    run = (await h.rows(ProcessRun, ProcessRun.task_id == tid))[0]
    assert (verdict.meta["verdict"], verdict.meta["process_run_id"]) == ("accept", run.id)
    assert (reply.content, reply.meta["process_run_id"], reply.meta["parts"]) == (folded, run.id, 1)


async def test_aura_and_the_evaluator_see_one_memory_block_with_the_dialogue(h):
    first_ask, first_reply = "Remember: my test code word is ORCA-19.", "Noted: your test code word is ORCA-19."
    second_ask, second_reply = "What's my test code word?", "Your test code word is ORCA-19."
    h.intake, h.drafts = [{"decision": "create_fresh"}], [first_reply]
    await h.finish((await h.send(first_ask, "1790000012.000100"))["task_id"])
    h.intake, h.drafts = [{"decision": "create_fresh"}], [second_reply]
    tid = (await h.send(second_ask, "1790000012.000200"))["task_id"]
    await h.finish(tid)

    prompt = h.prompts[-1]
    # One memory block (the prompt policy also names it once in its instructions).
    assert prompt.count(curator.HEADER) == 1 and prompt.count("RECENT DIALOGUE (") == 1
    assert f"Kirill: {first_ask}" in prompt and f"Aura: {first_reply}" in prompt
    assert f"Kirill: {first_ask}" in h.judged[-1], "the evaluator judges against the same dialogue"
    assert h.slack[-1] == second_reply
    fast = await h.rows(Event, Event.entity_id == tid, Event.event_type == "memory.fast_context")
    assert fast and "dialogue" in fast[0].event_payload["sections"]


async def test_a_retried_plan_step_is_told_why_its_last_attempt_was_not_accepted(h):
    answer = "Kobe beef lunch sets at Mouriya start around 5,000 yen."
    h.intake, h.drafts, h.bare = [{"decision": "create_fresh"}], ["", answer], {1}
    tid = (await h.send("What does a Kobe beef lunch cost?", "1790000013.000100"))["task_id"]
    assert (await h.finish(tid))["final_result"] == answer
    assert len(h.prompts) == 2 and "attempt 1 not accepted]: Output validation failed" in h.prompts[1]


async def test_the_same_dm_delivered_twice_runs_once(h):
    ask = "Compare the JR Pass with regional passes for Kansai."
    reply = "For Kansai alone the Kansai Area Pass costs far less than the nationwide JR Pass."
    duplicate = {}

    async def same_dm_again(payload):
        duplicate.update(await h.send(ask, "1790000003.000100"))

    h.intake, h.drafts, h.during = [{"decision": "create_fresh"}], [reply], {1: same_dm_again}
    tid = (await h.send(ask, "1790000003.000100"))["task_id"]
    await h.finish(tid)

    assert duplicate == {"task_id": tid, "status": "running", "deduplicated": True}
    assert [t.id for t in await h.rows(Task)] == [tid] and len(await h.rows(TaskIntakeDecision)) == 1
    assert h.slack == [reply] and len(h.prompts) == 1


async def test_stop_during_a_task_ends_it_without_delivering_the_draft(h):
    ask = "Write a detailed guide to Kyoto's temples."
    draft = "Kinkaku-ji, Ginkaku-ji and Kiyomizu-dera are the three temples to see first."
    h.intake, h.drafts, h.during = [{"decision": "create_fresh"}], [draft], {1: lambda payload: h.stop()}

    tid = (await h.send(ask, "1790000004.000100"))["task_id"]
    result = await h.finish(tid)

    assert result == {"status": "stopped_by_user", "task_id": tid}
    assert h.slack == [f"Task {tid[:8]} stopped as requested."]
    run = (await h.rows(ProcessRun, ProcessRun.task_id == tid))[0]
    assert (await h.task(tid)).status == "stopped_by_user" and run.current_state == "stopped_by_user"
    assert h.judged == [] and "evaluator.verdict" not in await h.events(tid)
    assert await h.said(tid, "user") == [ask, "stop"] and await h.said(tid, "assistant") == h.slack


async def test_a_long_answer_reaches_slack_in_ordered_parts_with_one_ledger_row(h):
    long = "\n\n".join(
        f"Part {i}. " + " ".join(f"Kansai cooking note {i}.{j} covers dashi, street food and markets." for j in range(9))
        for i in range(1, 9)
    )
    h.intake, h.drafts = [{"decision": "create_fresh"}], [long]

    tid = (await h.send("Summarize the history of Kansai cuisine in depth.", "1790000005.000100"))["task_id"]
    await h.finish(tid)

    assert len(long) > SLACK_PART_CHARS and len(h.slack) > 1
    assert all(len(part) <= SLACK_PART_CHARS for part in h.slack) and "\n\n".join(h.slack) == long
    assert await h.said(tid, "assistant") == [long] and Counter(await h.events(tid))["slack.delivered"] == 1
    receipts = Counter(r.effect_type for r in await h.rows(SideEffectReceipt))
    assert receipts == {"slack": 1, "slack.part": len(h.slack)}


async def test_a_permanent_slack_error_is_recorded_once_and_fails_the_task_with_that_reason(h, monkeypatch):
    h.slack_error = "channel_not_found"
    h.intake = [{"decision": "create_fresh"}]
    h.drafts = ["Hello, the heater in my flat stopped working on Monday. Could you send someone this week?"]

    tid = (await h.send("Draft a note to my landlord about the heater.", "1790000006.000100"))["task_id"]
    result = await h.finish(tid)

    assert (result["status"], result["reason"]) == ("failed", "slack_delivery_failed")
    task, run = await h.task(tid), (await h.rows(ProcessRun, ProcessRun.task_id == tid))[0]
    assert (task.status, task.supplementary_context["closed_reason"]) == ("failed", "slack_delivery_failed")
    assert run.current_state == "failed_terminal"
    assert len(h.slack) == 1
    failures = await h.rows(Event, Event.event_type == "slack.delivery_failed")
    assert [(e.entity_id, e.event_payload["error"]) for e in failures] == [(tid, "channel_not_found")]
    assert "slack.delivered" not in await h.events(tid) and await h.said(tid, "assistant") == []
    assert await h.rows(SideEffectReceipt) == []
    checks = await invariants_once_settled(monkeypatch)
    assert checks["slack_delivery"].status == "fail" and checks["slack_delivery"].details["task_ids"] == [tid]


async def test_a_message_that_arrives_while_the_reply_posts_starts_over_in_kirills_own_words(h):
    ask, late = "Find me a ramen place near Namba.", "And book a table for Saturday night."
    reply = "Ichiran Namba is a five-minute walk from the station."
    answer = "I can't book tables, but Ichiran Namba takes walk-ins on Saturday nights."

    async def late_message_arrives():
        h.intake.append({"decision": "attach_active", "target_task_id": tid})
        await h.send(late, "1790000007.000200")
        h.intake.append({"decision": "create_fresh"})

    h.intake, h.drafts = [{"decision": "create_fresh"}], [reply, answer]
    tid = (await h.send(ask, "1790000007.000100"))["task_id"]
    h.while_posting = late_message_arrives
    assert (await h.finish(tid))["final_result"] == reply

    [again] = [t for t in await h.rows(Task, Task.status != "cancelled") if t.id != tid]
    await h.finish(again.id)
    assert again.goal == late and await h.said(again.id, "user") == [late]
    assert f"User Request: {late}\n" in h.prompts[1] and f"continue task {tid}" not in h.prompts[1]
    assert Counter(await h.events(tid))["task.messages_resubmitted"] == 1
    assert h.slack == [reply, f"Got it: adding \u201c{late}\u201d to the task I'm working on ({tid[:8]}).", answer]


async def test_a_one_word_answer_reaches_slack_on_the_first_attempt(h):
    h.intake, h.drafts = [{"decision": "create_fresh"}], ["Pong!"]

    tid = (await h.send("ping", "1790000010.000100"))["task_id"]

    assert (await h.finish(tid))["final_result"] == "Pong!"
    assert h.slack == ["Pong!"] and len(h.prompts) == 1


async def test_a_conversational_reply_is_kept_in_process_memory_after_it_reaches_slack(h):
    reply = "Doing well, thanks. The Osaka plan is ready whenever you want it."
    h.intake, h.drafts = [{"decision": "create_fresh"}], [reply]

    tid = (await h.send("Hi Aura, how are you?", "1790000009.000100"))["task_id"]
    await h.finish(tid)

    run = (await h.rows(ProcessRun, ProcessRun.task_id == tid))[0]
    episodic = await h.rows(MemoryItem, MemoryItem.scope_id == run.id, MemoryItem.memory_type == "episodic")
    assert h.slack == [reply] and [m.content for m in episodic] == [reply]


async def test_a_reconciler_nudge_is_neither_folded_in_nor_resubmitted(h):
    ask, reply = "What's the best day trip from Osaka?", "Nara: the deer park and Todai-ji are an hour away by train."

    async def nudge(payload):
        handle = h.env.client.get_workflow_handle(f"workflow-{payload['task_id']}")
        await handle.signal("user_input", "[RECONCILER] Continue or report status.")

    h.intake, h.drafts, h.during = [{"decision": "create_fresh"}], [reply], {1: nudge}
    tid = (await h.send(ask, "1790000008.000100"))["task_id"]

    assert (await h.finish(tid))["final_result"] == reply and h.slack == [reply]
    assert len(h.prompts) == 1 and [t.id for t in await h.rows(Task, Task.status != "cancelled")] == [tid]
    assert "task.messages_resubmitted" not in await h.events(tid)


async def test_a_recall_that_adds_after_the_reply_is_noticed_refined_judged_and_sent(h, monkeypatch):
    ask, reply = "What should I pack for Osaka in October?", "Pack light layers and a rain jacket."
    refined = "I recalled that you travel carry-on only, so: light layers and a packable rain jacket."
    found = {"relevant": True, "brief": "Kirill travels carry-on only.", "tasks": [], "sections": [], "gaps": [],
             "facts": [{"statement": "Kirill travels carry-on only.", "status": "current", "as_of": "2026-09-02",
                        "citations": ["fact:f1"]}]}
    config.update_settings(lambda s: s.setdefault("deep_memory", {}).update(recall_enabled=True, followups_enabled=True))

    async def read(query, dialogue, evidence):
        return found, {"input_tokens": 900, "output_tokens": 80, "model": "gpt-6-luna"}

    async def novelty(query, answer, report):
        return {"verdict": "adds", "points": ["Kirill travels carry-on only."], "reason": "packing"}, {"model": "gpt-6-luna"}

    async def release_the_recall():
        h.recall_released.set()

    monkeypatch.setattr(recall, "read", read)
    monkeypatch.setattr(recall, "judge_novelty", novelty)
    h.intake, h.drafts, h.while_posting = [{"decision": "create_fresh"}], [reply, refined], release_the_recall
    tid = (await h.send(ask, "1790000013.000100"))["task_id"]
    result = await h.finish(tid)

    assert result == {"status": "completed", "task_id": tid, "final_result": reply, "followup": refined}
    assert h.slack == [reply, RECALL_NOTICE, refined]
    log = await h.rows(TaskMessage, TaskMessage.task_id == tid, order=TaskMessage.created_at)
    assert [(m.role, m.kind) for m in log] == [
        ("user", "request"), ("evaluator", "verdict"), ("assistant", "reply"),
        ("assistant", "notice"), ("evaluator", "verdict"), ("assistant", "followup"),
    ]
    assert log[-1].meta["attempt"] == 2 and log[-2].meta["verdict"] == "accept"
    assert h.sessions_used == ["", "__recall"] and h.verdict_sessions == [1, 2]
    assert "WHAT YOUR MEMORY ADDS:\n- Kirill travels carry-on only." in h.prompts[1]
    assert "FOLLOW-UP:" in h.judged[1] and reply in h.judged[1]
    [report] = await h.rows(DeepContextReport, DeepContextReport.task_id == tid)
    assert (report.status, report.consumed_by, report.novelty["verdict"]) == ("ready", "followup", "adds")
    assert (await h.task(tid)).status == "completed"
    events = Counter(await h.events(tid))
    assert (events["evaluator.accept"], events["deep_recall.novelty"], events["deep_recall.followup"]) == (2, 1, 1)
    checks = await invariants_once_settled(monkeypatch)
    assert {name: c.status for name, c in checks.items()} == dict.fromkeys(checks, "pass")
