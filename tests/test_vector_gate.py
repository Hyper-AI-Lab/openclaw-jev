"""Vector similarity is advisory evidence only — never assigns a workflow."""
from app.task_registry.vector_gate import advisory_vector_hits, vector_similarity_gate


def _high_score_ctx():
    return {
        "vector_similar": [
            {
                "task_id": "abc",
                "score": 0.95,
                "intent_snippet": "self-upgrade and add a plugin",
                "process_type": "tool_self_upgrade",
            }
        ],
        "active_tasks": [
            {
                "task_id": "abc",
                "session_key": "agent:main:main",
                "task_kind": "one_shot",
            }
        ],
        "recent_registry": [],
    }


def test_vector_gate_never_auto_attaches():
    ctx = _high_score_ctx()
    result = vector_similarity_gate(ctx, session_key="agent:main:main")
    assert result is None
    assert ctx["advisory_hits"]
    assert ctx["advisory_hits"][0]["task_id"] == "abc"
    assert ctx["advisory_hits"][0]["advisory"] is True
    assert ctx["advisory_hits"][0]["above_threshold"] is True


def test_vector_gate_never_wait_or_create_guided():
    ctx = {
        "vector_similar": [
            {"task_id": "done-1", "score": 0.99, "intent_snippet": "deploy"}
        ],
        "active_tasks": [],
        "recent_registry": [
            {
                "task_id": "done-1",
                "outcome_summary": "Prior run finished with full deployment checklist.",
                "goal": "deploy app",
            }
        ],
    }
    assert vector_similarity_gate(ctx, session_key="agent:main:main") is None
    assert ctx["advisory_hits"][0]["task_id"] == "done-1"


def test_vector_gate_no_hits_still_none():
    ctx = {"vector_similar": [], "active_tasks": []}
    assert vector_similarity_gate(ctx) is None
    assert ctx["advisory_hits"] == []


def test_advisory_hits_flag_below_threshold():
    ctx = {
        "vector_similar": [{"task_id": "low", "score": 0.1}],
        "active_tasks": [],
    }
    hits = advisory_vector_hits(ctx)
    assert hits[0]["above_threshold"] is False
    assert hits[0]["advisory"] is True
