"""Intake LLM is the catalog adjudicator — keywords are soft only."""
from app.task_registry.intake_decision_engine import apply_intake_policy
from app.task_registry.intake_prompt import build_intake_prompt
from app.orchestrator.web_capability import merge_web_into_intake
from app.workflows.catalog import soft_catalog_candidates


AWARENESS = (
    "I added some functionality for you: now you can self-upgrade and add a "
    "plugin to your arsenal. Are you aware of that?"
)


def test_llm_null_catalog_hint_wins_over_keyword_rich_intent():
    result = apply_intake_policy(
        {
            "decision": "create_fresh",
            "confidence": 90,
            "execution_mode": "conversational",
            "catalog_hint": None,
            "rationale": "awareness check, not requesting an upgrade",
        },
        {"intent": AWARENESS, "active_tasks": [], "task_type": "user"},
        tags=["user-request"],
    )
    assert result["catalog_type"] is None
    assert result["execution_mode"] == "conversational"


def test_llm_catalog_hint_accepted_without_forcing_via_task_type_hack():
    result = apply_intake_policy(
        {
            "decision": "create_fresh",
            "confidence": 92,
            "execution_mode": "structured_work",
            "catalog_hint": "coding_task",
            "rationale": "user asked for a weather plugin in Aura's own code",
        },
        {
            "intent": "Please self-upgrade and add a weather plugin to your arsenal",
            "active_tasks": [],
            "task_type": "user",
        },
        tags=["user-request"],
    )
    assert result["catalog_type"] == "coding_task"
    assert "catalog_from_intake_llm" in result["policy_overrides"]


def test_llm_alias_catalog_hint_normalized():
    result = apply_intake_policy(
        {
            "decision": "create_fresh",
            "confidence": 88,
            "execution_mode": "structured_work",
            "catalog_hint": "self_upgrade",
            "rationale": "alias",
        },
        {"intent": "run self upgrade please", "active_tasks": [], "task_type": "user"},
        tags=["user-request"],
    )
    assert result["catalog_type"] == "coding_task"


def test_soft_candidates_advisory_for_awareness():
    hits = soft_catalog_candidates(AWARENESS)
    assert "coding_task" in hits


def test_intake_prompt_includes_soft_candidates_and_adjudicator_rules():
    prompt = build_intake_prompt({"intent": AWARENESS, "active_tasks": []})
    assert "soft_catalog_candidates" in prompt
    assert "ADVISORY ONLY" in prompt
    assert "coding_task: Kirill explicitly asks for a reviewed coding job" in prompt
    # Every other change to Aura's code is her own work with Claude Code, through a pull request.
    assert "Any other request to change Aura's own code is structured_work with catalog_hint\n  null" in prompt


def test_merge_web_interact_does_not_hard_assign_catalog():
    decision = {
        "decision": "create_fresh",
        "catalog_type": None,
        "guidance_notes": "",
        "policy_overrides": [],
    }
    out = merge_web_into_intake(
        decision,
        "open the browser and take a screenshot of moltmarket",
    )
    assert out["catalog_type"] is None
    assert any(
        str(o).startswith("web_capability_soft_catalog_hint:")
        for o in out["policy_overrides"]
    )
