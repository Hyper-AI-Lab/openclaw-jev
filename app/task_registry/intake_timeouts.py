"""Intake timeout constants — safe inside Temporal workflow sandbox (no file I/O)."""
from __future__ import annotations

# Keep intake snappy: fail/rotate quickly rather than blocking Slack create for a minute.
DEFAULT_INTAKE_LLM_TIMEOUT_SEC = 20
DEFAULT_INTAKE_CONTEXT_SEC = 10


def intake_timeout_budget(
    llm_sec: int = DEFAULT_INTAKE_LLM_TIMEOUT_SEC,
    context_sec: int = DEFAULT_INTAKE_CONTEXT_SEC,
) -> dict:
    """Aligned intake timeouts.

    ``context_sec`` covers bounded vector/DB context assembly that runs before
    the OpenClaw intake LLM turn (and may consume its full deadline).
    """
    llm = max(10, int(llm_sec))
    context = max(0, int(context_sec))
    return {
        "llm_sec": llm,
        "context_sec": context,
        "openclaw_poll_sec": max(10, llm - 5),
        # Context + LLM poll + small scheduling buffer.
        "activity_start_to_close_sec": llm + context + 20,
        "activity_heartbeat_sec": max(55, llm + 5),
        "workflow_execution_sec": llm + context + 45,
    }
