"""Step 3: intake evidence pack; vector gate cannot force catalog/attach."""
from unittest.mock import AsyncMock, patch

import pytest

from app.activities.intake_activities import run_intake_deterministic_gates
from app.task_registry.intake_decision_engine import apply_intake_policy
from app.task_registry.intake_prompt import build_intake_prompt
from app.task_registry.vector_gate import vector_similarity_gate

AWARENESS = (
    "I added some functionality for you: now you can self-upgrade and add a "
    "plugin to your arsenal. Are you aware of that?"
)


def test_high_vector_score_on_awareness_does_not_force_attach():
    ctx = {
        "intent": AWARENESS,
        "session_key": "agent:main:main",
        "vector_similar": [
            {
                "task_id": "upgrade-1",
                "score": 0.97,
                "process_type": "tool_self_upgrade",
                "intent_snippet": "self-upgrade add plugin",
            }
        ],
        "active_tasks": [
            {
                "task_id": "upgrade-1",
                "session_key": "agent:main:main",
                "task_kind": "one_shot",
                "process_type": "tool_self_upgrade",
            }
        ],
        "recent_registry": [],
        "task_type": "user",
    }
    assert vector_similarity_gate(ctx, session_key="agent:main:main") is None
    result = apply_intake_policy(
        {
            "decision": "create_fresh",
            "confidence": 90,
            "execution_mode": "conversational",
            "catalog_hint": None,
            "rationale": "awareness, not an upgrade request",
        },
        ctx,
        tags=["user-request"],
    )
    assert result["catalog_type"] is None
    assert result["decision"] == "create_fresh"
    assert result["execution_mode"] == "conversational"


def test_intake_prompt_includes_memory_and_evidence_pack():
    prompt = build_intake_prompt(
        {
            "intent": AWARENESS,
            "active_tasks": [],
            "memory_hits": [
                {
                    "memory_id": "mem-42",
                    "snippet": "User prefers MiniMax M3 and hates native Slack fallback.",
                    "source": "memory",
                }
            ],
            "evidence_pack": [
                {
                    "task_id": "t-1",
                    "source": "fts",
                    "snippet": "prior awareness reply",
                    "score": 0.4,
                }
            ],
            "advisory_hits": [
                {"task_id": "upgrade-1", "score": 0.97, "advisory": True}
            ],
        }
    )
    assert "User prefers MiniMax M3" in prompt
    assert "mem-42" in prompt
    assert "evidence_pack" in prompt
    assert "prior awareness reply" in prompt
    assert "advisory_hits" in prompt
    assert "FOUR RELATION CLASSES" in prompt
    assert "clarify" in prompt
    assert "ADVISORY ONLY" in prompt


def test_the_analyst_reads_the_recent_dialogue_and_answers_recall_depth():
    prompt = build_intake_prompt({
        "intent": "Which one was the cheapest?",
        "active_tasks": [],
        "recent_dialogue": ["[21:03] Kirill: find me three hotels in Osaka",
                            "[21:05] Aura: Hotel A 12,000 yen, Hotel B 9,500 yen, Hotel C 15,000 yen.",
                            "[21:06] Kirill: 一番安いのはどれ？"],
    })
    assert "Hotel B 9,500 yen" in prompt and "一番安いのはどれ？" in prompt
    assert prompt.index('"recent_dialogue"') < prompt.index('"active_tasks"')
    assert '"recall_depth": "none|deep' in prompt and "- recall_depth: deep when" in prompt


def test_intake_prompt_carries_full_raw_text_not_500_chars():
    long_intent = "please review this spec: " + ("alpha " * 400)
    assert len(long_intent) > 500
    prompt = build_intake_prompt({"intent": long_intent, "active_tasks": []})
    assert long_intent[:800] in prompt


@pytest.mark.asyncio
async def test_deterministic_gates_do_not_return_vector_assignment():
    ctx = {
        "intent": AWARENESS,
        "session_key": "agent:main:main",
        "active_tasks": [
            {
                "task_id": "upgrade-1",
                "session_key": "agent:main:main",
                "task_kind": "one_shot",
            }
        ],
        "vector_similar": [{"task_id": "upgrade-1", "score": 0.99}],
        "task_type": "user",
    }
    with patch(
        "app.activities.intake_activities.fast_path_decision",
        return_value=None,
    ), patch(
        "app.activities.intake_activities.skip_valid_decision",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.activities.intake_activities.skip_noop_decision",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.activities.intake_activities.supersede_decision",
        new=AsyncMock(return_value=None),
    ):
        out = await run_intake_deterministic_gates(
            {"intent": AWARENESS, "session_key": "agent:main:main", "tags": ["user-request"]},
            ctx,
            recurrence_key=None,
            fp="x",
        )
    assert out is None
    assert ctx.get("advisory_hits")


@pytest.mark.asyncio
async def test_assemble_intake_context_exposes_hybrid_pack():
    from app.task_registry.intake_context import assemble_intake_context

    retrieval = {
        "active_tasks": [{"task_id": "t1"}],
        "recent_registry": [],
        "vector_similar": [{"task_id": "t2", "score": 0.8}],
        "evidence_pack": [{"task_id": "t1", "source": "active", "snippet": "running"}],
        "memory_hits": [{"memory_id": "m1", "snippet": "prefers RMP Slack"}],
        "fts_hits": [{"task_id": "t3", "source": "fts", "snippet": "lexical hit"}],
    }
    with patch(
        "app.task_registry.intake_context.hybrid_search_bounded",
        new=AsyncMock(return_value=retrieval),
    ), patch(
        "app.task_registry.intake_context.list_task_messages",
        new=AsyncMock(return_value=[]),
    ):
        result = await assemble_intake_context(AWARENESS, session_key="s1")
    assert result["memory_hits"][0]["snippet"] == "prefers RMP Slack"
    assert result["evidence_pack"][0]["source"] == "active"
    assert result["fts_hits"][0]["task_id"] == "t3"
    assert result["intent"] == AWARENESS
