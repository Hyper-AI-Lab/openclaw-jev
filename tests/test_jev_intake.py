"""Jev intake cascade: state, composition, modes and hooks. No live provider, database or cache writes."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from app.activities import intake_activities
from app.decisions import intake, memory
from app.decisions.jev import MODEL, JevClient, Policy

NOW = datetime(2026, 9, 28, 5, 0, tzinfo=timezone.utc)
SLACK = "agent:main:slack:channel:u0aelfytlks"
RUN_ID = "11111111-1111-4111-8111-111111111111"
DONE_ID = "33333333-3333-4333-8333-333333333333"
OTHER_ID = "44444444-4444-4444-8444-444444444444"
ALIASES = {"R1": RUN_ID, "F1": DONE_ID}
STATUS_PING = {"relation": "running", "execution_mode": "conversational", "catalog": "none", "web_intent": "none",
    "running_target": "R1", "running_action": "asks_status", "finished_target": "none"}


def context(intent="How is the invoice export going?", running=True, finished=True):
    active = [
        {"task_id": RUN_ID, "status": "running", "session_key": SLACK, "task_kind": "one_shot",
         "goal": "Export March invoices to CSV", "goal_snippet": "Export March invoices to CSV",
         "updated_at": (NOW - timedelta(minutes=3)).replace(tzinfo=None).isoformat()},
        {"task_id": OTHER_ID, "status": "running", "session_key": "agent:main:slack:channel:someone-else",
         "task_kind": "one_shot", "goal": "Another conversation", "updated_at": None},
    ] if running else []
    registry = [
        {"task_id": DONE_ID, "terminal_status": "completed", "process_type": "user",
         "intent_snippet": "Summarize the Hermes skill catalog", "outcome_summary": "Sent a 5-point summary",
         "task_ended_at": (NOW - timedelta(days=2)).replace(tzinfo=None).isoformat()},
        {"task_id": "c" * 36, "terminal_status": "completed", "process_type": "canary",
         "intent_snippet": "RMP CANARY health check", "outcome_summary": "CANARY_OK", "task_ended_at": None},
    ] if finished else []
    return {"intent": intent, "session_key": SLACK, "active_tasks": active, "recent_registry": registry,
        "memory_hits": [{"snippet": "Kirill prefers metric units"}], "tags": ["user-request"], "task_type": "user"}


def answer(choice, confidence=.97, is_max=True, probabilities=None):
    return {"type": "choice", "choice": choice, "confidence": confidence,
        "probabilities": probabilities or {choice: confidence}, "choice_is_max": is_max}


def answers(**overrides):
    base = {k: answer(v) for k, v in STATUS_PING.items()}
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def jev_body(questions, choices, confidence=.97):
    out = {}
    for qid, q in questions.items():
        labels = list(q["criteria"])
        rest = (1 - confidence) / (len(labels) - 1)
        out[qid] = {"type": "choice", "choice": choices[qid], "confidence": confidence,
            "probabilities": {label: confidence if label == choices[qid] else rest for label in labels}}
    return {"model": MODEL, "answers": out, "usage": {"input_tokens": 900, "output_tokens": 0}}


@pytest.fixture
def provider(monkeypatch):
    """Wire intake to a MockTransport client; returns the list of outbound requests."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-test-key")
    monkeypatch.setattr(intake, "_recent_dialogue", AsyncMock(return_value=["Kirill: please export March invoices"]))
    calls = []

    def install(mode, handler=None):
        def default(req):
            calls.append(req)
            return httpx.Response(200, json=jev_body(json.loads(req.content)["questions"], STATUS_PING))
        client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(handler or default)))
        monkeypatch.setattr(intake, "get_client", lambda: client)
        monkeypatch.setattr(intake, "get_policy", lambda: Policy(intake_mode=mode, cache_ttl_sec=0))
        return client

    install.calls = calls
    return install


def test_state_uses_aliases_and_drops_distractors():
    state, questions, aliases = intake.build_intake_request(
        context(), ["[21:03] Kirill: please export March invoices"], now=NOW)
    wire = json.dumps({"state": state, "questions": questions})
    assert aliases == ALIASES
    assert RUN_ID not in wire and DONE_ID not in wire and OTHER_ID not in wire and "CANARY" not in wire
    assert state["running_tasks"][0]["last_update"] == "updated in the last 10 minutes"
    assert state["finished_tasks"][0]["ended"] == "ended within the last week"
    assert state["recent_dialogue"] == ["Kirill: please export March invoices"]
    assert set(questions) == {"relation", "execution_mode", "catalog", "web_intent",
        "running_target", "running_action", "finished_target"}
    assert set(questions["catalog"]["criteria"]) == set(intake.CATALOG_RUBRIC) | {"none"}


def test_questions_follow_available_candidates():
    _, questions, aliases = intake.build_intake_request(context(running=False, finished=False), [], now=NOW)
    assert set(questions) == {"relation", "execution_mode", "catalog", "web_intent"} and aliases == {}


def test_task_text_in_criteria_is_redacted():
    ctx = context()
    ctx["active_tasks"][0]["goal_snippet"] = "Log in with password=hunter2-never-send"
    _, questions, _ = intake.build_intake_request(ctx, [], now=NOW)
    assert "hunter2-never-send" not in json.dumps(questions)


@pytest.mark.parametrize("overrides,decision,target,similar", [
    ({"running_action": answer("add_instructions")}, "attach_active", RUN_ID, [RUN_ID]),
    ({}, "wait_active", RUN_ID, [RUN_ID]),
    ({"running_action": answer("wants_restart")}, "rebuild_stale", RUN_ID, [RUN_ID]),
    ({"relation": answer("finished"), "finished_target": answer("F1")}, "create_guided", None, [DONE_ID]),
    ({"relation": answer("memory")}, "create_guided", None, []),
    ({"relation": answer("new")}, "create_fresh", None, []),
    ({"relation": answer("finished"), "finished_target": answer("none")}, "create_fresh", None, []),
])
def test_composition_maps_onto_intake_decisions(overrides, decision, target, similar):
    result = intake.compose_intake_result(answers(**overrides), ALIASES, Policy())
    assert result["decision"] == decision and result["target_task_id"] == target
    assert result["similar_task_ids"] == similar and result["decision_source"] == "jev"
    assert result["confidence"] == 97 and result["relation_class"] in ("running", "finished", "memory", "new")


def test_running_decisions_do_not_need_an_execution_mode():
    result = intake.compose_intake_result(answers(execution_mode=answer("conversational", .1)), ALIASES, Policy())
    assert result["decision"] == "wait_active" and result["execution_mode"] is None


def test_split_between_new_and_finished_is_a_fresh_task_in_the_same_conversation():
    split = answer("new", .2, probabilities={"new": .55, "finished": .42, "running": .02, "unclear": .01})
    no_running = answers(relation=split, running_target=None, running_action=None)
    result = intake.compose_intake_result(no_running, ALIASES, Policy())
    assert result["decision"] == "create_fresh" and result["confidence"] == 97


@pytest.mark.parametrize("overrides", [
    {"relation": answer("unclear")},
    {"relation": answer("new", .8, probabilities={"new": .8, "running": .15, "unclear": .05})},
    {"relation": answer("new", .88, probabilities={"new": .88, "finished": .02, "unclear": .1})},
    {"relation": answer("new"), "execution_mode": answer("conversational", .8)},
    {"running_target": answer("none")},
    {"running_target": answer("R1", .9)},
    {"relation": answer("running", .9)},
    {"running_target": None, "running_action": None},
    {"relation": answer("new", is_max=False)},
])
def test_composition_abstains_to_the_llm(overrides):
    assert intake.compose_intake_result(answers(**overrides), ALIASES, Policy()) is None


@pytest.mark.parametrize("mode,catalog,expected", [
    ("structured_work", answer("login", .95), "login"),
    ("structured_work", answer("login", .85), None),
    ("conversational", answer("login", .99), None),
])
def test_catalog_needs_structured_work_and_high_confidence(mode, catalog, expected):
    result = intake.compose_intake_result(
        answers(relation=answer("new"), execution_mode=answer(mode), catalog=catalog), ALIASES, Policy())
    assert result["catalog_hint"] == expected


def test_confidence_is_the_weakest_required_answer_and_web_intent_is_gated():
    relation = answer("new", .9, probabilities={"new": .9, "finished": .03, "running": .05, "unclear": .02})
    result = intake.compose_intake_result(
        answers(relation=relation, web_intent=answer("search", .5)), ALIASES, Policy())
    assert result["confidence"] == 93 and result["web_intent"] is None


async def test_off_internal_and_empty_messages_never_call(provider):
    provider("off", lambda req: pytest.fail("outbound request"))
    assert await intake.review_intake(context(), tags=["user-request"]) == (None, None)
    provider("enforce", lambda req: pytest.fail("outbound request"))
    assert await intake.review_intake(context(), tags=["canary"]) == (None, None)
    assert await intake.review_intake(context(intent="  "), tags=[]) == (None, None)


async def test_shadow_records_without_changing_the_path(provider):
    client = provider("shadow")
    use, record = await intake.review_intake(context(), tags=["user-request"])
    assert use is None and record["accepted"] and record["proposal"]["decision"] == "wait_active"
    assert record["answers"]["relation"] == {"choice": "running", "confidence": .97}
    sent = provider.calls[0].content.decode()
    assert len(provider.calls) == 1 and RUN_ID not in sent and "offline-test-key" not in sent
    await client.aclose()


async def test_enforce_returns_the_llm_result_shape(provider):
    client = provider("enforce")
    use, record = await intake.review_intake(context(), tags=["user-request"])
    assert use["decision"] == "wait_active" and use["target_task_id"] == RUN_ID and use["jev"] == record
    await client.aclose()


async def test_provider_outage_and_consumer_errors_leave_intake_alone(provider, monkeypatch):
    client = provider("enforce", lambda req: httpx.Response(529))
    use, record = await intake.review_intake(context(), tags=["user-request"])
    assert use is None and record["status"] == "unavailable" and not record["accepted"]
    monkeypatch.setattr(intake, "build_intake_request", lambda *a, **k: {}["missing"])
    assert await intake.review_intake(context(), tags=["user-request"]) == (
        None, {"mode": "enforce", "status": "unavailable", "reason": "consumer_error"})
    await client.aclose()


@pytest.fixture
def hooks(monkeypatch):
    ctx = context()
    monkeypatch.setattr(intake_activities, "_build_intake_context", AsyncMock(return_value=(ctx, None, "fp-jev-test")))
    monkeypatch.setattr(intake_activities, "run_intake_deterministic_gates", AsyncMock(return_value=None))
    monkeypatch.setattr(intake_activities, "get_cached", lambda *a: None)
    monkeypatch.setattr(intake_activities, "set_cached", lambda *a: None)
    monkeypatch.setattr("app.task_registry.intake_decision_engine.get_task_registry_intake_mode", lambda: "enforce")
    llm = AsyncMock(return_value='{"decision": "create_fresh", "confidence": 80, "rationale": "new", "execution_mode": "conversational"}')
    monkeypatch.setattr("app.activities.openclaw_activities._execute_intake_llm", llm)
    record = {"mode": "enforce", "status": "ok", "accepted": True}
    proposal = {**intake.compose_intake_result(answers(), ALIASES, Policy()), "jev": record}

    def jev(result):
        monkeypatch.setattr(intake, "review_intake", AsyncMock(return_value=result))
    return {"llm": llm, "jev": jev, "proposal": proposal, "record": record,
        "payload": {"intent": ctx["intent"], "session_key": SLACK, "tags": ["user-request"], "task_type": "user"}}


async def test_accepted_jev_decision_skips_the_openclaw_turn(hooks):
    hooks["jev"]((hooks["proposal"], hooks["record"]))
    result = await intake_activities.classify_task_intake(hooks["payload"])
    hooks["llm"].assert_not_awaited()
    assert result["decision"] == "wait_active" and result["target_task_id"] == RUN_ID
    assert result["llm_raw"]["decision_source"] == "jev" and result["llm_raw"]["jev"] == hooks["record"]


async def test_abstain_or_shadow_keeps_the_llm_and_records_jev(hooks):
    hooks["jev"]((None, hooks["record"]))
    result = await intake_activities.classify_task_intake(hooks["payload"])
    hooks["llm"].assert_awaited_once()
    assert result["decision"] == "create_fresh" and result["llm_raw"]["jev"] == hooks["record"]


async def test_api_fallback_uses_an_accepted_jev_decision(hooks):
    hooks["jev"]((hooks["proposal"], hooks["record"]))
    result = await intake_activities.classify_task_intake_deterministic(hooks["payload"])
    assert result["decision"] == "wait_active" and result["confidence"] == 97
    assert "degraded_intake_create_fresh" not in result["policy_overrides"]


async def test_api_fallback_without_jev_stays_degraded(hooks):
    hooks["jev"]((None, hooks["record"]))
    result = await intake_activities.classify_task_intake_deterministic(hooks["payload"])
    assert result["decision"] == "create_fresh" and result["confidence"] == 0
    assert result["llm_raw"]["jev"] == hooks["record"]


def test_committed_intake_fixtures_validate():
    from pathlib import Path
    from ops import jev_eval

    fixture = Path(__file__).resolve().parent / "fixtures" / "jev_intake_eval.jsonl"
    cases, unlabeled = jev_eval.load_intake_cases([fixture], 250)
    assert len(cases) >= 50 and unlabeled == 0


async def test_intake_eval_flags_harmful_attach_and_fails_the_gate(monkeypatch):
    from pathlib import Path
    from ops import jev_eval
    from app.decisions.jev import Evaluation

    fixture = Path(__file__).resolve().parent / "fixtures" / "jev_intake_eval.jsonl"
    cases = [c for c in jev_eval.load_intake_cases([fixture], 250)[0] if c["id"] in ("run-status-export", "ambig-two-exports")]
    picks = {"run-status-export": {**STATUS_PING},
        "ambig-two-exports": {**STATUS_PING, "running_action": "add_instructions", "finished_target": None}}

    async def evaluate(state, questions, *, purpose, rubric, policy):
        chosen = picks[purpose.removeprefix("eval.")]
        return Evaluation("ok", result={"usage": {"input_tokens": 800, "output_tokens": 0},
            "answers": {qid: answer(chosen[qid]) for qid in questions}}, latency_ms=300.0)
    monkeypatch.setattr(jev_eval, "get_client", lambda: type("Fake", (), {"evaluate": staticmethod(evaluate)})())
    monkeypatch.setattr(jev_eval, "close_jev_client", AsyncMock())
    monkeypatch.setattr(jev_eval, "MIN_REQUEST_SPACING_SEC", 0)
    report = await jev_eval.run_intake(cases, Policy(cache_ttl_sec=0))
    by_id = {r["id"]: r for r in report["records"]}
    assert by_id["run-status-export"]["correct"] and not by_id["run-status-export"]["harmful"]
    assert by_id["ambig-two-exports"]["harmful"] and "decision" in by_id["ambig-two-exports"]["errors"]
    assert report["intake"]["accuracy_on_accepted"] == .5 and report["intake"]["harmful_errors"] == 1
    assert report["intake"]["abstain_expected"] == 1 and report["intake"]["abstained_when_expected"] == 0
    assert report["gate"]["passed"] is False


async def test_promotion_review_sees_only_facts_that_pass_validation(monkeypatch):
    from app.memory import promotion

    good = {"content": "Always use metric units in all my future tasks.", "confidence": 85, "kind": "constraint_fact"}
    weak = {"content": "A weakly supported guess about the user", "confidence": 50, "kind": "constraint_fact"}
    monkeypatch.setattr(promotion, "extract_semantic_facts", lambda content, process_type: [good, weak])
    review = AsyncMock(return_value={"mode": "enforce", "allowed_indices": [], "held_indices": [0]})
    monkeypatch.setattr(memory, "review_promotions", review)
    stats = await promotion.promote_completion_memory(process_run_id="p", process_type="", task_id="t",
        episodic_content="Kirill asked Aura to always use metric units in all future tasks.")
    assert review.await_args.args[1] == [good]
    assert stats["rejected"] == 1 and stats["jev_held"] == 1 and stats["promoted_semantic"] == 0
