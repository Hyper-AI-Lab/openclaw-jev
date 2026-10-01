"""How an approval gate reads Kirill's reply: his own words only, and only a whole-message decision.

A gate's signal carries the task's catch-up brief before Kirill's message (``with_catchup``). Reading
the whole signal let a brief that mentioned "stop" or "ok" decide; now only ``user_words`` count.
"""
from __future__ import annotations

import re
from datetime import timedelta

from app.orchestrator.process_brief import user_words
from app.task_registry.stop_command import is_whole_message_stop

APPROVE_WORDS = frozenset({"approve", "approved", "deploy"})
GATE_STOP_WORDS = frozenset({"reject", "rejected", "deny"})
REMINDER_AFTER = timedelta(hours=12)
CLOSE_AFTER = timedelta(days=7)
_EDGES = re.compile(r"^[\s.!?,:;]+|[\s.!?,:;]+$")


def gate_decision(signalled: str) -> str:
    """"approve", "stop" or "other" for one reply to a gate."""
    words = user_words(signalled)
    if is_whole_message_stop(words):
        return "stop"
    normalized = _EDGES.sub("", words).lower()
    if normalized in GATE_STOP_WORDS:
        return "stop"
    return "approve" if normalized in APPROVE_WORDS else "other"
