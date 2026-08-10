import hashlib
import time

from app.api.server import (
    ACTIVE_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    _fresh_idempotency_key,
    _make_idempotency_key,
)


class TaskRequest:
    def __init__(self, **kwargs):
        self.intent = kwargs.get("intent", "")
        self.raw_text = kwargs.get("raw_text", "")
        self.session_key = kwargs.get("session_key", "agent:main:main")
        self.idempotency_key = kwargs.get("idempotency_key")


def test_active_and_terminal_status_sets_disjoint():
    assert not (ACTIVE_TASK_STATUSES & TERMINAL_TASK_STATUSES)


def test_fresh_idempotency_key_differs():
    base = "abc123"
    k1 = _fresh_idempotency_key(base)
    time.sleep(0.001)
    k2 = _fresh_idempotency_key(base)
    assert k1 != k2
    assert k1 != base


def test_make_idempotency_key_from_raw_text():
    req = TaskRequest(
        session_key="agent:main:main",
        raw_text="hello",
        intent="[AUTO-ROUTED]: hello",
    )
    k1 = _make_idempotency_key(req)
    k2 = hashlib.sha256(b"agent:main:main:hello").hexdigest()
    assert k1 == k2


def test_terminal_prior_gets_fresh_key():
    from app.api import server as srv

    base = hashlib.sha256(b"agent:main:main:hello").hexdigest()
    fresh = srv._fresh_idempotency_key(base)
    assert fresh != base
