import pytest

from app.evidence import check_catalog_completion, check_evidence, evidence_high_confidence


def test_evidence_passes_valid_response():
    result = check_evidence(
        "What is the URL for Moltbook?",
        "The Moltbook site is at https://moltbook.com",
    )
    assert result["passed"] is True
    assert result["issues"] == []


def test_evidence_fails_entity_mismatch():
    result = check_evidence(
        "Tell me about Moltbook",
        "MoltMarket is a great platform for jobs.",
    )
    assert result["passed"] is False
    assert any("MoltMarket" in i for i in result["issues"])


def test_evidence_fails_empty():
    result = check_evidence("Do something", "   ")
    assert result["passed"] is False


def test_catalog_registration_requires_account_signals():
    result = check_catalog_completion(
        "account_registration",
        "Register on example.com",
        "Done.",
    )
    assert result["passed"] is False

    result_ok = check_catalog_completion(
        "account_registration",
        "Register on example.com",
        "Account created successfully with username kirill@example.com",
    )
    assert result_ok["passed"] is True


def test_evidence_high_confidence_conversational_greeting():
    intent = "How are you today, aura?"
    response = (
        "I'm doing well, Kirill! Everything is running smoothly. "
        "How can I help you this afternoon?"
    )
    assert evidence_high_confidence(intent, response) is True


def test_evidence_high_confidence_conversational_short_fails():
    assert evidence_high_confidence("How are you?", "Fine.") is False
