from app.activities.side_effects import slack_idempotency_key


def test_slack_idempotency_key_stable():
    key1 = slack_idempotency_key("task-abc", "Hello world")
    key2 = slack_idempotency_key("task-abc", "Hello world")
    assert key1 == key2
    assert key1.startswith("slack:task-abc:")


def test_slack_idempotency_key_differs_by_message():
    key1 = slack_idempotency_key("task-abc", "Hello")
    key2 = slack_idempotency_key("task-abc", "Goodbye")
    assert key1 != key2


def test_slack_idempotency_key_differs_by_task():
    key1 = slack_idempotency_key("task-a", "Hello")
    key2 = slack_idempotency_key("task-b", "Hello")
    assert key1 != key2
