from app.notification_policy import (
    format_workflow_error,
    is_canary_intent,
    is_internal_task,
    is_silent_system_ack,
    should_deliver_slack,
    strip_system_acks,
)


def test_canary_is_internal():
    assert is_canary_intent("RMP CANARY: Reply with exactly CANARY_OK")
    assert is_internal_task("RMP CANARY: test", "canary", ["canary", "system"])
    assert is_internal_task(
        "Intake attach smoke 20260608: summarize briefly",
        "user",
        ["intake-smoke"],
    )
    assert not should_deliver_slack(
        "RMP CANARY: test", "canary", ["canary"], "CANARY_OK"
    )


def test_user_task_delivers():
    assert should_deliver_slack(
        "What do you remember?", "user", ["user-request"], "Here is what I recall."
    )


def test_silent_acks():
    assert is_silent_system_ack("CANARY_OK")
    assert is_silent_system_ack("HEARTBEAT_OK")
    assert not is_silent_system_ack("Hello Kirill")


def test_format_timeout_error():
    class Cause(Exception):
        pass

    class Wrapper(Exception):
        pass

    err = Wrapper("Activity task failed")
    err.__cause__ = Cause("Timed out waiting for agent reply.")
    msg = format_workflow_error(err)
    assert "did not finish in time" in msg


def test_format_rate_limit():
    msg = format_workflow_error(Exception("429 status code rate limit"))
    assert "rate limit" in msg.lower()


def test_strip_system_acks_prefix():
    assert (
        strip_system_acks("HEARTBEAT_OK CANARY_OK I'm operational. Ready when you are.")
        == "I'm operational. Ready when you are."
    )
    assert strip_system_acks("CANARY_OK") == ""
    assert strip_system_acks("Hello Kirill") == "Hello Kirill"


def test_sanitize_user_facing_text():
    from app.notification_policy import sanitize_user_facing_text

    raw = (
        "I'm here, Kirill!\n\n"
        '{"facts": {"step_complete": true, "stopped": false}}'
    )
    assert sanitize_user_facing_text(raw) == "I'm here, Kirill!"
    meta = (
        "Hello\nOrigin: Slack DM\nSession: main\n"
        "Goal: be helpful\nEmotional state: calm\nEOF"
    )
    assert sanitize_user_facing_text(meta) == "Hello"
    dup = "Short reply.\n\nShort reply."
    assert sanitize_user_facing_text(dup) == "Short reply."


def test_plan_json_never_delivered_to_slack():
    from app.notification_policy import sanitize_user_facing_text

    plan = (
        '{"steps": [{"name": "Explain tooling", "kind": "deliver", '
        '"predicate_id": "deliver", "prompt": "Explain web_search."}]}'
    )
    assert sanitize_user_facing_text(plan) == ""
    assert not should_deliver_slack(
        "How do you search the web?", "user", ["user-request"], plan
    )
