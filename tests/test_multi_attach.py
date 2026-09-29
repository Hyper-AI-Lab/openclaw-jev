"""A message that adds to several running tasks attaches to each of them."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.decisions import intake
from app.decisions.jev import Policy
from app.task_registry.intake_decision_engine import apply_intake_policy
from app.task_registry.intake_handlers import acknowledge_attach, handle_intake_outcome

SLACK = "agent:main:slack:channel:u0aelfytlks"
A, B, C = "a" * 36, "b" * 36, "c" * 36


def answer(choice, confidence=0.97):
    return {"type": "choice", "choice": choice, "confidence": confidence,
            "probabilities": {choice: confidence}, "choice_is_max": True}


def running_answers(action="add_instructions", **extra):
    base = {"relation": answer("running"), "execution_mode": answer("structured_work"),
            "catalog": answer("none"), "web_intent": answer("none"),
            "running_target": answer("R1"), "running_action": answer(action)}
    base.update(extra)
    return base


def active(task_id, goal, session=SLACK):
    return {"task_id": task_id, "status": "running", "session_key": session, "task_kind": "one_shot",
            "goal": goal, "goal_snippet": goal, "task_type": "user", "updated_at": None}


def test_each_running_task_gets_its_own_question_when_there_are_several():
    ctx = {"intent": "use the Q3 numbers for both", "session_key": SLACK, "recent_registry": [],
           "active_tasks": [active(A, "Draft the investor update"), active(B, "Build the Q3 dashboard")]}
    _, questions, aliases = intake.build_intake_request(ctx, [])
    assert {"adds_to_R1", "adds_to_R2"} <= set(questions) and aliases == {"R1": A, "R2": B}
    ctx["active_tasks"] = ctx["active_tasks"][:1]
    _, questions, _ = intake.build_intake_request(ctx, [])
    assert not any(q.startswith("adds_to_") for q in questions)


def test_jev_attaches_every_running_task_the_message_clearly_adds_to():
    aliases = {"R1": A, "R2": B}
    both = intake.compose_intake_result(
        running_answers(adds_to_R1=answer("yes"), adds_to_R2=answer("yes")), aliases, Policy())
    assert both["decision"] == "attach_active" and both["target_task_ids"] == [A, B]
    unsure = intake.compose_intake_result(
        running_answers(adds_to_R1=answer("yes"), adds_to_R2=answer("yes", 0.8)), aliases, Policy())
    assert unsure["target_task_ids"] == [A]


def test_status_and_restart_questions_stay_on_one_task():
    aliases = {"R1": A, "R2": B}
    for action in ("asks_status", "wants_restart"):
        result = intake.compose_intake_result(
            running_answers(action, adds_to_R2=answer("yes")), aliases, Policy())
        assert result["target_task_ids"] == [A]


def test_policy_keeps_only_active_same_conversation_extra_targets():
    ctx = {"intent": "use the Q3 numbers for both", "session_key": SLACK,
           "active_tasks": [active(A, "Draft the investor update"), active(B, "Build the Q3 dashboard"),
                            active(C, "Another chat", session="agent:main:slack:channel:someone-else")]}
    llm = {"decision": "attach_active", "confidence": 96, "target_task_id": A,
           "target_task_ids": [A, B, C, "d" * 36]}
    with patch("app.task_registry.intake_decision_engine.get_task_registry_intake_mode", return_value="enforce"):
        result = apply_intake_policy(llm, ctx, tags=["user-request"])
    assert result["decision"] == "attach_active" and result["target_task_ids"] == [A, B]
    assert "cross_session_extra_target_dropped" in result["policy_overrides"]


async def test_handler_records_and_signals_each_target():
    db = MagicMock()
    rows = MagicMock()
    rows.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=rows)
    decision = {"effective_decision": "attach_active", "decision": "attach_active", "target_task_id": A,
                "target_task_ids": [A, B], "intake_mode": "enforce", "request_hash": "h", "confidence": 96}
    with patch("app.task_registry.intake_handlers.record_intake_decision", new=AsyncMock(return_value="dec-1")), \
         patch("app.task_registry.intake_handlers.build_catchup_block", new=AsyncMock(return_value="")), \
         patch("app.task_registry.intake_handlers.add_task_message", new=AsyncMock()) as add_msg:
        out = await handle_intake_outcome(decision, request=None, db=db, intent="use the Q3 numbers for both",
                                          session_key=SLACK, tags=["user-request"])
    assert out["intake_action"] == "attach_active" and out["target_task_ids"] == [A, B]
    assert [s["task_id"] for s in out["signals"]] == [A, B]
    assert [c.args[0] for c in add_msg.await_args_list] == [A, B]
    assert [c.args[0].entity_id for c in db.add.call_args_list if getattr(c.args[0], "event_type", "") == "intake.attach"] == [A, B]


@pytest.mark.parametrize("ids,where", [([A], "the task I'm working on (aaaaaaaa)"),
                                       ([A, B], "the tasks I'm working on (aaaaaaaa, bbbbbbbb)")])
async def test_acknowledgement_names_the_tasks_and_quotes_the_message(ids, where):
    with patch("app.task_registry.intake_handlers._intake_notify_slack", new=AsyncMock()) as notify:
        await acknowledge_attach(session_key=SLACK, task_ids=ids, intent="use the Q3 numbers for both", tags=[])
    message = notify.await_args.kwargs["message"]
    assert "\u201cuse the Q3 numbers for both\u201d" in message and where in message
