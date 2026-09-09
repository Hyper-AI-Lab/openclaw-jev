"""Intake handler outcome tests."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.task_registry.intake_decision_engine import apply_intake_policy
from app.task_registry.intake_handlers import handle_intake_outcome
from app.task_registry.intake_prompt import parse_intake_response


def _empty_exec():
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    scalars = MagicMock()
    scalars.first.return_value = None
    scalars.all.return_value = []
    result.scalars.return_value = scalars
    return result


NOTIFY = "app.task_registry.intake_handlers._intake_notify_slack"
RECORD = "app.task_registry.intake_handlers.record_intake_decision"
ADD_MSG = "app.task_registry.intake_handlers.add_task_message"


@pytest.mark.asyncio
async def test_handle_wait_active():
    db = AsyncMock()
    db.add = MagicMock()
    decision = {
        "effective_decision": "wait_active",
        "decision": "wait_active",
        "target_task_id": "task-1",
        "intake_mode": "enforce",
        "request_hash": "abc",
        "confidence": 90,
        "rationale": "dup",
    }
    with patch(RECORD, new=AsyncMock(return_value="dec-1")), patch(
        NOTIFY, new=AsyncMock()
    ) as slack:
        out = await handle_intake_outcome(
            decision,
            request=None,
            db=db,
            intent="hello",
            session_key="agent:main:main",
            tags=["user-request"],
        )
    assert out["intake_action"] == "wait_active"
    assert out["task_id"] == "task-1"
    slack.assert_awaited()


@pytest.mark.asyncio
async def test_handle_skip():
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    decision = {
        "effective_decision": "skip_noop",
        "decision": "skip_noop",
        "intake_mode": "enforce",
        "request_hash": "abc",
        "confidence": 90,
        "rationale": "noop",
    }
    with patch(RECORD, new=AsyncMock(return_value="dec-1")), patch(
        NOTIFY, new=AsyncMock()
    ) as slack, patch(ADD_MSG, new=AsyncMock(return_value="m1")):
        out = await handle_intake_outcome(
            decision,
            request=None,
            db=db,
            intent="hello",
            session_key="agent:main:main",
            tags=[],
        )
    assert out["skipped"] is True
    assert out["task_id"]
    assert out["status"] == "completed"
    slack.assert_awaited()


@pytest.mark.asyncio
async def test_handle_records_execution_mode_in_event():
    db = AsyncMock()
    db.add = MagicMock()
    decision = {
        "effective_decision": "create_fresh",
        "decision": "create_fresh",
        "intake_mode": "enforce",
        "request_hash": "abc",
        "confidence": 90,
        "rationale": "chat",
        "execution_mode": "conversational",
        "llm_raw": {"execution_mode": "conversational"},
    }
    recorded = {}

    async def _record(**kwargs):
        recorded.update(kwargs)
        return "dec-1"

    with patch(RECORD, new=AsyncMock(side_effect=_record)):
        out = await handle_intake_outcome(
            decision,
            request=None,
            db=db,
            intent="hello",
            session_key="agent:main:main",
            tags=["user-request"],
        )
    assert out["execution_mode"] == "conversational"
    assert recorded["llm_raw"]["execution_mode"] == "conversational"
    event = db.add.call_args_list[0][0][0]
    assert event.event_payload["execution_mode"] == "conversational"
    assert "same conversation" in (out.get("_guided_memory_block") or "")
    assert "not amnesia" in (out.get("_guided_memory_block") or "")


@pytest.mark.asyncio
async def test_handle_supersede_terminates_and_falls_through():
    db = AsyncMock()
    db.add = MagicMock()
    old_task = MagicMock()
    old_task.status = "running"
    old_task.next_check_at = None
    task_result = MagicMock()
    task_result.scalar_one_or_none.return_value = old_task
    db.execute = AsyncMock(return_value=task_result)
    decision = {
        "effective_decision": "supersede",
        "decision": "supersede",
        "target_task_id": "old-task-1",
        "intake_mode": "enforce",
        "request_hash": "abc",
        "confidence": 95,
        "rationale": "stale failed",
    }
    with patch(RECORD, new=AsyncMock(return_value="dec-1")), patch(
        "app.temporal_control.terminate_task_workflow",
        new=AsyncMock(return_value=True),
    ) as mock_term:
        out = await handle_intake_outcome(
            decision,
            request=None,
            db=db,
            intent="[cron:test] retry job",
            session_key="agent:main:cron",
            tags=["cron"],
        )
    assert out is None
    mock_term.assert_awaited_once_with("old-task-1", "superseded by intake")
    assert old_task.status == "failed"


@pytest.mark.asyncio
async def test_handle_clarify_no_temporal_start():
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    decision = {
        "effective_decision": "clarify",
        "decision": "clarify",
        "intake_mode": "enforce",
        "request_hash": "abc",
        "confidence": 40,
        "rationale": "unsure",
        "guidance_notes": "Do you mean the upgrade, or just checking awareness?",
    }
    with patch(RECORD, new=AsyncMock(return_value="dec-1")), patch(
        NOTIFY, new=AsyncMock()
    ) as slack, patch(ADD_MSG, new=AsyncMock(return_value="m1")):
        out = await handle_intake_outcome(
            decision,
            request=None,
            db=db,
            intent="are you aware you can self-upgrade?",
            session_key="agent:main:main",
            tags=["user-request"],
        )
    assert out["intake_action"] == "clarify"
    assert out["status"] == "pending_user_input"
    assert out["workflow_started"] is False
    slack.assert_awaited()
    created = db.add.call_args_list[1][0][0]
    assert created.status == "pending_user_input"
    assert created.supplementary_context["intake_clarify"] is True


@pytest.mark.asyncio
async def test_handle_rebuild_stale_terminates_and_returns_memory():
    db = AsyncMock()
    db.add = MagicMock()
    old_task = MagicMock()
    old_task.status = "blocked"
    old_task.next_check_at = None
    old_task.goal = "stuck upgrade"
    old_task.task_type = "tool_self_upgrade"
    old_task.supplementary_context = {}
    task_result = MagicMock()
    task_result.scalar_one_or_none.return_value = old_task
    scalars = MagicMock()
    scalars.first.return_value = None
    scalars.all.return_value = []
    task_result.scalars.return_value = scalars
    db.execute = AsyncMock(return_value=task_result)
    decision = {
        "effective_decision": "rebuild_stale",
        "decision": "rebuild_stale",
        "target_task_id": "stuck-1",
        "intake_mode": "enforce",
        "request_hash": "abc",
        "confidence": 80,
        "rationale": "workflow dead",
    }
    with patch(RECORD, new=AsyncMock(return_value="dec-1")), patch(
        "app.temporal_control.terminate_task_workflow",
        new=AsyncMock(return_value=True),
    ) as mock_term:
        out = await handle_intake_outcome(
            decision,
            request=None,
            db=db,
            intent="try the upgrade again",
            session_key="agent:main:main",
            tags=["user-request"],
        )
    mock_term.assert_awaited_once_with("stuck-1", "rebuild_stale by intake")
    assert out is not None
    assert out.get("intake_action") is None
    assert "rebuild_stale" in out["_guided_memory_block"]
    assert old_task.status == "failed"


@pytest.mark.asyncio
async def test_handle_attach_includes_catchup_signal():
    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(return_value=_empty_exec())
    decision = {
        "effective_decision": "attach_active",
        "decision": "attach_active",
        "target_task_id": "live-1",
        "intake_mode": "enforce",
        "request_hash": "abc",
        "confidence": 90,
        "rationale": "same work",
    }
    with patch(RECORD, new=AsyncMock(return_value="dec-1")), patch(
        ADD_MSG, new=AsyncMock(return_value="m1")
    ):
        out = await handle_intake_outcome(
            decision,
            request=None,
            db=db,
            intent="also check the logs",
            session_key="agent:main:main",
            tags=["user-request"],
        )
    assert out["intake_action"] == "attach_active"
    assert out["signal_required"] is True
    assert "also check the logs" in out["signal_text"]
    assert "PROCESS BRIEF" in out["signal_text"]


@pytest.mark.asyncio
async def test_handle_attach_resume_clarify_starts_existing():
    db = AsyncMock()
    db.add = MagicMock()
    pending = MagicMock()
    pending.status = "pending_user_input"
    pending.task_type = "user"
    pending.goal = "original ask"
    pending.supplementary_context = {
        "intake_clarify": True,
        "original_intent": "original ask",
        "clarify_question": "which one?",
    }
    task_result = MagicMock()
    task_result.scalar_one_or_none.return_value = pending
    scalars = MagicMock()
    scalars.first.return_value = None
    scalars.all.return_value = []
    task_result.scalars.return_value = scalars
    db.execute = AsyncMock(return_value=task_result)
    decision = {
        "effective_decision": "attach_active",
        "decision": "attach_active",
        "target_task_id": "clarify-1",
        "intake_mode": "enforce",
        "request_hash": "abc",
        "confidence": 90,
        "rationale": "answer",
    }
    with patch(RECORD, new=AsyncMock(return_value="dec-1")), patch(
        ADD_MSG, new=AsyncMock(return_value="m1")
    ):
        out = await handle_intake_outcome(
            decision,
            request=None,
            db=db,
            intent="the awareness check, not an upgrade",
            session_key="agent:main:main",
            tags=["user-request"],
        )
    assert out["intake_action"] == "resume_clarify"
    assert out["signal_required"] is False
    assert pending.status == "created"
    assert "CLARIFY ANSWER" in out["_guided_memory_block"]


def test_low_confidence_without_active_is_clarify():
    result = apply_intake_policy(
        {"decision": "create_fresh", "confidence": 20, "rationale": "guess"},
        {"intent": "hmm", "active_tasks": [], "session_key": "agent:main:main"},
        tags=["user-request"],
    )
    assert result["decision"] == "clarify"
    assert "low_confidence_clarify" in result["policy_overrides"]


def test_low_confidence_single_active_is_wait():
    result = apply_intake_policy(
        {"decision": "create_fresh", "confidence": 20, "rationale": "guess"},
        {
            "intent": "hmm",
            "session_key": "agent:main:main",
            "active_tasks": [
                {
                    "task_id": "only-1",
                    "session_key": "agent:main:main",
                    "status": "running",
                    "task_kind": "one_shot",
                }
            ],
        },
        tags=["user-request"],
    )
    assert result["decision"] == "wait_active"
    assert result["target_task_id"] == "only-1"
    assert "low_confidence_wait" in result["policy_overrides"]


def test_degraded_intake_does_not_wait_on_health_canary():
    """Kirill ping during hourly canary: intake LLM cancelled, canary on agent:main:main."""
    result = apply_intake_policy(
        {
            "decision": "create_fresh",
            "confidence": 0,
            "rationale": "Intake workflow unavailable; deterministic fallback only",
            "execution_mode": "conversational",
        },
        {
            "intent": "Are you here aura?",
            "session_key": "agent:main:main",
            "task_type": "user",
            "active_tasks": [
                {
                    "task_id": "98919271-c322-41bf-a84c-3979bd1ea879",
                    "session_key": "agent:main:main",
                    "status": "running",
                    "task_kind": "recurrent",
                    "task_type": "canary",
                    "goal": "RMP CANARY: Reply with exactly CANARY_OK on its own line. No tools.",
                }
            ],
        },
        tags=["user-request"],
    )
    assert result["decision"] == "create_fresh"
    assert result["effective_decision"] == "create_fresh"
    assert result["target_task_id"] is None
    assert "degraded_intake_create_fresh" in result["policy_overrides"]
    assert "low_confidence_wait" not in result["policy_overrides"]
    assert result["execution_mode"] == "conversational"


def test_wait_active_targeting_canary_is_ignored_for_user_dm():
    result = apply_intake_policy(
        {
            "decision": "wait_active",
            "confidence": 90,
            "rationale": "canary running",
            "target_task_id": "canary-1",
            "execution_mode": "conversational",
        },
        {
            "intent": "Are you here aura?",
            "session_key": "agent:main:main",
            "task_type": "user",
            "active_tasks": [
                {
                    "task_id": "canary-1",
                    "session_key": "agent:main:main",
                    "status": "running",
                    "task_type": "canary",
                    "goal": "RMP CANARY: Reply with exactly CANARY_OK",
                }
            ],
        },
        tags=["user-request"],
    )
    assert result["decision"] == "create_fresh"
    assert "internal_active_ignored" in result["policy_overrides"]


def test_low_confidence_many_actives_is_clarify():
    result = apply_intake_policy(
        {"decision": "create_fresh", "confidence": 10, "rationale": "guess"},
        {
            "intent": "hmm",
            "session_key": "agent:main:main",
            "active_tasks": [
                {
                    "task_id": "a",
                    "session_key": "agent:main:main",
                    "status": "running",
                    "task_kind": "one_shot",
                },
                {
                    "task_id": "b",
                    "session_key": "agent:main:main",
                    "status": "running",
                    "task_kind": "one_shot",
                },
            ],
        },
        tags=["user-request"],
    )
    assert result["decision"] == "clarify"


def test_canary_clarify_becomes_fresh():
    result = apply_intake_policy(
        {"decision": "clarify", "confidence": 40, "rationale": "unsure"},
        {"intent": "RMP CANARY", "active_tasks": []},
        tags=["canary"],
    )
    assert result["decision"] == "create_fresh"
    assert "canary_never_skip" in result["policy_overrides"]


def test_pending_clarify_followup_attaches():
    result = apply_intake_policy(
        {"decision": "create_fresh", "confidence": 90, "rationale": "new"},
        {
            "intent": "the first one",
            "session_key": "agent:main:main",
            "active_tasks": [
                {
                    "task_id": "q-1",
                    "session_key": "agent:main:main",
                    "status": "pending_user_input",
                    "intake_clarify": True,
                    "task_kind": "one_shot",
                }
            ],
        },
        tags=["user-request"],
    )
    assert result["decision"] == "attach_active"
    assert result["target_task_id"] == "q-1"
    assert "clarify_followup_attach" in result["policy_overrides"]


def test_parse_failure_is_clarify_not_fresh():
    parsed = parse_intake_response("not json at all")
    assert parsed["decision"] == "clarify"
    assert parsed["confidence"] == 0
