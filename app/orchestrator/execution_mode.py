"""Execution mode — single intake classification for workflow plan shaping."""
from __future__ import annotations

from typing import Any, Iterable, Optional

EXECUTION_CONVERSATIONAL = "conversational"
EXECUTION_STRUCTURED = "structured_work"

VALID_EXECUTION_MODES = frozenset({EXECUTION_CONVERSATIONAL, EXECUTION_STRUCTURED})

INTAKE_LLM_FAILURE_MARKERS = (
    "intake llm error",
    "not in activity context",
    "intake workflow unavailable",
)


def intake_llm_failed(llm_result: Optional[dict]) -> bool:
    if not llm_result:
        return False
    if int(llm_result.get("confidence") or 0) > 0:
        return False
    rationale = (llm_result.get("rationale") or "").lower()
    return any(m in rationale for m in INTAKE_LLM_FAILURE_MARKERS)


def infer_degraded_execution_mode(
    *,
    intent: str,
    tags: Optional[Iterable[str]] = None,
    task_type: str = "",
    catalog_type: Optional[str] = None,
    intake_llm_failed_flag: bool = False,
) -> str:
    """Failure-mode default when intake LLM did not classify execution_mode."""
    if not intake_llm_failed_flag:
        return EXECUTION_STRUCTURED

    if catalog_type:
        return EXECUTION_STRUCTURED

    tag_set = {str(t).lower() for t in (tags or [])}
    if tag_set & {"canary", "system", "memory-canary", "cron", "heartbeat"}:
        return EXECUTION_STRUCTURED

    if (task_type or "").lower() in ("canary", "cron", "heartbeat"):
        return EXECUTION_STRUCTURED

    if (task_type or "").lower() == "user":
        return EXECUTION_CONVERSATIONAL

    return EXECUTION_STRUCTURED


def normalize_execution_mode(value: Any) -> Optional[str]:
    if not value:
        return None
    mode = str(value).strip().lower()
    if mode in VALID_EXECUTION_MODES:
        return mode
    if mode in ("chat", "conversation", "conversational"):
        return EXECUTION_CONVERSATIONAL
    if mode in ("work", "structured", "task", "research"):
        return EXECUTION_STRUCTURED
    return None


def resolve_execution_mode(
    *,
    intent: str,
    tags: Optional[Iterable[str]] = None,
    task_type: str = "",
    llm_mode: Any = None,
    catalog_type: Optional[str] = None,
    llm_result: Optional[dict] = None,
) -> str:
    """
    Pick plan execution mode once at intake time.

    Intake LLM ``execution_mode`` wins when valid. When intake LLM failed,
    degraded policy favors conversational for user Slack DMs.
    """
    if catalog_type:
        return EXECUTION_STRUCTURED

    normalized = normalize_execution_mode(llm_mode)
    if normalized:
        return normalized

    if intake_llm_failed(llm_result):
        return infer_degraded_execution_mode(
            intent=intent,
            tags=tags,
            task_type=task_type,
            catalog_type=catalog_type,
            intake_llm_failed_flag=True,
        )

    tag_set = {str(t).lower() for t in (tags or [])}
    if tag_set & {"canary", "system", "memory-canary"}:
        return EXECUTION_STRUCTURED

    if (task_type or "").lower() in ("canary", "cron", "heartbeat"):
        return EXECUTION_STRUCTURED

    return EXECUTION_STRUCTURED
