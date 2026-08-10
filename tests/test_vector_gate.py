"""Layer 2 vector similarity gate tests."""
from app.task_registry.vector_gate import vector_similarity_gate


def test_vector_gate_active_same_session_attach():
    ctx = {
        "vector_similar": [{"task_id": "abc", "score": 0.85}],
        "active_tasks": [
            {
                "task_id": "abc",
                "session_key": "agent:main:main",
                "task_kind": "one_shot",
            }
        ],
        "recent_registry": [],
    }
    result = vector_similarity_gate(ctx, session_key="agent:main:main")
    assert result is not None
    assert result["decision"] == "attach_active"
    assert result["target_task_id"] == "abc"


def test_vector_gate_cross_session_wait():
    ctx = {
        "vector_similar": [{"task_id": "abc", "score": 0.9}],
        "active_tasks": [
            {
                "task_id": "abc",
                "session_key": "agent:other:main",
                "task_kind": "one_shot",
            }
        ],
        "recent_registry": [],
    }
    result = vector_similarity_gate(ctx, session_key="agent:main:main")
    assert result is not None
    assert result["decision"] == "wait_active"


def test_vector_gate_durable_cross_session_attach():
    ctx = {
        "vector_similar": [{"task_id": "abc", "score": 0.9}],
        "active_tasks": [
            {
                "task_id": "abc",
                "session_key": "agent:other:main",
                "task_kind": "durable",
            }
        ],
        "recent_registry": [],
    }
    result = vector_similarity_gate(ctx, session_key="agent:main:main")
    assert result is not None
    assert result["decision"] == "attach_active"


def test_vector_gate_no_hits():
    assert vector_similarity_gate({"vector_similar": [], "active_tasks": []}) is None


def test_vector_gate_create_guided_uses_registry_outcome():
    ctx = {
        "vector_similar": [
            {"task_id": "done-1", "score": 0.88, "intent_snippet": "short snippet"}
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
    result = vector_similarity_gate(ctx, session_key="agent:main:main")
    assert result is not None
    assert result["decision"] == "create_guided"
    assert "deployment checklist" in result["guidance_notes"]
