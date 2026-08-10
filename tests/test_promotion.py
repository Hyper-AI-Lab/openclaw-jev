from app.memory.promotion import extract_semantic_facts, validate_fact


def test_extract_semantic_facts_from_url():
    facts = extract_semantic_facts(
        "Registration complete at https://example.com/register ok",
        "account_registration",
    )
    assert len(facts) >= 1
    assert any("example.com" in f["content"] for f in facts)


def test_validate_fact_rejects_low_confidence():
    ok, reason = validate_fact({"content": "x" * 20, "confidence": 50})
    assert ok is False
    assert reason == "low_confidence"
