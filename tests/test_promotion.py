from app.memory.promotion import build_procedure_summary, validate_fact


def test_validate_fact_rejects_low_confidence():
    ok, reason = validate_fact({"content": "x" * 20, "confidence": 50})
    assert ok is False
    assert reason == "low_confidence"


def test_a_procedure_needs_several_steps_and_tools():
    steps = [{"name": "search"}, {"name": "deliver"}]
    tools = [{"tool": "web_fetch", "ok": True}]
    assert build_procedure_summary("Compare rail passes", steps, [], "done") is None
    assert build_procedure_summary("Compare rail passes", steps[:1], tools, "done") is None
    assert build_procedure_summary("Compare rail passes", steps, tools, "done").startswith("Task: Compare rail passes")
