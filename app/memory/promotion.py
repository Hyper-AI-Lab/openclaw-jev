"""Memory promotion pipeline: extract → validate → promote (report §6.1)."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

MIN_PROMOTION_CONFIDENCE = 70


def extract_semantic_facts(content: str, process_type: str = "") -> List[Dict[str, Any]]:
    """Stage B: derive candidate semantic facts from episodic text (deterministic heuristics)."""
    if not content or len(content.strip()) < 30:
        return []

    facts: List[Dict[str, Any]] = []
    text = content.strip()

    # URL / domain facts
    for url in re.findall(r"https?://[^\s\)\]>\"']+", text):
        try:
            from urllib.parse import urlparse

            domain = urlparse(url).netloc.lower()
            if domain:
                facts.append(
                    {
                        "content": f"Site referenced: {domain} ({url[:120]})",
                        "confidence": 85,
                        "kind": "environment_fact",
                    }
                )
        except Exception:
            pass

    # Account / registration outcomes
    if process_type == "account_registration" or "account" in text.lower():
        if any(k in text.lower() for k in ("registered", "account created", "verified")):
            facts.append(
                {
                    "content": f"Registration outcome ({process_type or 'generic'}): "
                    f"{text[:400]}",
                    "confidence": 75,
                    "kind": "outcome_fact",
                }
            )

    # Login session
    if process_type == "login" or "logged in" in text.lower():
        if any(k in text.lower() for k in ("logged in", "authenticated", "session")):
            facts.append(
                {
                    "content": f"Authentication outcome: {text[:400]}",
                    "confidence": 80,
                    "kind": "outcome_fact",
                }
            )

    # General constraint sentences (short declarative lines)
    for line in text.split("\n"):
        line = line.strip("- •*").strip()
        if 20 <= len(line) <= 200 and re.match(r"^[A-Z]", line):
            if any(
                kw in line.lower()
                for kw in ("must", "requires", "always", "never", "policy", "constraint")
            ):
                facts.append(
                    {"content": line, "confidence": 72, "kind": "constraint_fact"}
                )

    # Dedupe by content prefix
    seen = set()
    unique: List[Dict[str, Any]] = []
    for f in facts:
        key = f["content"][:100]
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique[:8]


def validate_fact(fact: Dict[str, Any]) -> Tuple[bool, str]:
    """Stage C: confidence and provenance quality gate."""
    conf = int(fact.get("confidence", 0))
    content = (fact.get("content") or "").strip()
    if conf < MIN_PROMOTION_CONFIDENCE:
        return False, "low_confidence"
    if len(content) < 15:
        return False, "too_short"
    if "[REDACTED" in content:
        return False, "redacted_content"
    return True, "ok"


async def promote_completion_memory(
    *,
    process_run_id: str,
    process_type: str,
    task_id: str,
    episodic_content: str,
    user_scope_id: str = "default",
) -> Dict[str, Any]:
    """Stage D+E: promote validated facts to semantic/procedural/pinned pools."""
    from app.memory.router import MemoryRouter
    from sqlalchemy import select

    from app.db.database import AsyncSessionLocal
    from app.db.models import MemoryItem

    stats = {
        "extracted": 0,
        "promoted_semantic": 0,
        "promoted_procedural": 0,
        "promoted_pinned": 0,
        "rejected": 0,
    }

    async def _exists(scope_type: str, scope_id: str, content_prefix: str) -> bool:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(MemoryItem.id).where(
                    MemoryItem.scope_type == scope_type,
                    MemoryItem.scope_id == scope_id,
                    MemoryItem.content.like(f"{content_prefix[:80]}%"),
                )
            )
            return result.scalar_one_or_none() is not None

    candidates = extract_semantic_facts(episodic_content, process_type)
    stats["extracted"] = len(candidates)

    for fact in candidates:
        ok, reason = validate_fact(fact)
        if not ok:
            stats["rejected"] += 1
            continue
        if await _exists("user", user_scope_id, fact["content"]):
            stats["rejected"] += 1
            continue
        try:
            await MemoryRouter.write(
                scope_type="user",
                scope_id=user_scope_id,
                memory_type="semantic",
                content=fact["content"],
                provenance={
                    "task_id": task_id,
                    "process_run_id": process_run_id,
                    "promotion_stage": "validated",
                    "kind": fact.get("kind"),
                },
                confidence=int(fact["confidence"]),
            )
            stats["promoted_semantic"] += 1
            if int(fact.get("confidence", 0)) >= 85:
                await MemoryRouter.write(
                    scope_type="user",
                    scope_id=user_scope_id,
                    memory_type="pinned",
                    content=fact["content"],
                    provenance={
                        "task_id": task_id,
                        "process_run_id": process_run_id,
                        "promotion_stage": "pinned",
                        "kind": fact.get("kind"),
                    },
                    confidence=int(fact["confidence"]),
                )
                stats["promoted_pinned"] += 1
        except ValueError:
            stats["rejected"] += 1

    if process_type and len(episodic_content.strip()) >= 50:
        try:
            await MemoryRouter.write(
                scope_type="procedural",
                scope_id=process_type,
                memory_type="procedural",
                content=episodic_content[:2000],
                provenance={
                    "task_id": task_id,
                    "promoted_from": "completion_pipeline",
                    "process_type": process_type,
                },
                confidence=85,
            )
            stats["promoted_procedural"] += 1
        except ValueError:
            stats["rejected"] += 1

    return stats
