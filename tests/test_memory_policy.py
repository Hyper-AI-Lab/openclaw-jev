from app.memory.policy import (
    apply_write_policy,
    is_scope_allowed,
    redact_secrets,
)


def test_redact_openai_api_key():
    content = "Use key sk-abcdefghijklmnopqrstuvwxyz1234567890 for auth"
    redacted = redact_secrets(content)
    assert "sk-" not in redacted
    assert "[REDACTED:api_key]" in redacted


def test_redact_slack_token():
    content = "token is xoxb-1234567890-abcdefghij"
    redacted = redact_secrets(content)
    assert "xoxb-" not in redacted
    assert "[REDACTED:slack_token]" in redacted


def test_redact_bearer_token():
    content = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
    redacted = redact_secrets(content)
    assert "eyJhbGci" not in redacted
    assert "Bearer [REDACTED]" in redacted


def test_redact_api_key_assignment():
    content = "config api_key=supersecretvalue123"
    redacted = redact_secrets(content)
    assert "supersecretvalue123" not in redacted


def test_scope_gate_procedural_only_on_procedural_scope():
    assert is_scope_allowed("procedural", "procedural") is True
    assert is_scope_allowed("process", "procedural") is False


def test_scope_gate_working_on_process():
    assert is_scope_allowed("process", "working") is True
    assert is_scope_allowed("user", "working") is False


def test_apply_write_policy_redacts_before_vector():
    allowed, reason, redacted = apply_write_policy(
        "process",
        "episodic",
        "Stored token sk-abcdefghijklmnopqrstuvwxyz1234567890",
    )
    assert allowed is True
    assert reason == "ok"
    assert "[REDACTED:api_key]" in redacted
    assert "sk-" not in redacted


def test_apply_write_policy_rejects_bad_scope():
    allowed, reason, _ = apply_write_policy(
        "process",
        "procedural",
        "playbook snippet",
    )
    assert allowed is False
    assert "not_allowed" in reason
