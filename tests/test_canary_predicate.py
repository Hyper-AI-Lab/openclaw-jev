from app.orchestrator.step_predicates import evaluate_step_predicate


def test_canary_ok_passes_deliver_predicate():
    result = evaluate_step_predicate(
        "deliver",
        user_intent="RMP CANARY: Reply with exactly CANARY_OK",
        agent_text="CANARY_OK",
    )
    assert result["passed"] is True
    assert result["suggested_status"] == "completed"
