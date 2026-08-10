"""One-shot vector memory seeding from workspace markdown and Postgres memories."""
import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from sqlalchemy import select

from app.config import get_vector_memory_config
from app.db.database import AsyncSessionLocal
from app.db.models import MemoryItem
from app.memory.vector import get_vector_service, reset_vector_service

logger = logging.getLogger("rmp.vector_seed")

WORKSPACE_ROOT = Path("/root/.openclaw/workspace")
WORKSPACE_FILES = ("USER.md", "MEMORY.md", "SOUL.md", "AGENTS.md")
MIN_CHUNK_CHARS = 80
MAX_CHUNK_CHARS = 1800
EMBED_PAUSE_SEC = 0.35


def _split_markdown(text: str) -> List[str]:
    sections = re.split(r"\n(?=#{1,3}\s)", text.strip())
    chunks: List[str] = []
    for section in sections:
        section = section.strip()
        if not section or len(section) < MIN_CHUNK_CHARS:
            continue
        if len(section) <= MAX_CHUNK_CHARS:
            chunks.append(section)
            continue
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", section) if p.strip()]
        buf = ""
        for para in paragraphs:
            candidate = f"{buf}\n\n{para}".strip() if buf else para
            if len(candidate) <= MAX_CHUNK_CHARS:
                buf = candidate
            else:
                if buf and len(buf) >= MIN_CHUNK_CHARS:
                    chunks.append(buf)
                buf = para
        if buf and len(buf) >= MIN_CHUNK_CHARS:
            chunks.append(buf)
    return chunks


def _workspace_sources() -> List[Tuple[Path, str]]:
    sources: List[Tuple[Path, str]] = []
    for name in WORKSPACE_FILES:
        path = WORKSPACE_ROOT / name
        if path.is_file():
            sources.append((path, "user"))
    memory_dir = WORKSPACE_ROOT / "memory"
    if memory_dir.is_dir():
        for path in sorted(memory_dir.glob("*.md")):
            sources.append((path, "user"))
    return sources


def seed_workspace(user_scope_id: str = "default") -> Dict[str, Any]:
    svc = get_vector_service()
    stats = {"files": 0, "chunks": 0, "indexed": 0, "errors": 0}
    for path, scope_type in _workspace_sources():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Could not read %s: %s", path, exc)
            stats["errors"] += 1
            continue
        stats["files"] += 1
        for chunk in _split_markdown(text):
            stats["chunks"] += 1
            ref = svc.add(
                scope_type,
                user_scope_id,
                chunk,
                "semantic",
                {"source": "workspace_seed", "path": str(path)},
            )
            if ref:
                stats["indexed"] += 1
            else:
                stats["errors"] += 1
            time.sleep(EMBED_PAUSE_SEC)
    return stats


async def seed_postgres_memories() -> Dict[str, Any]:
    svc = get_vector_service()
    stats = {"rows": 0, "indexed": 0, "errors": 0}
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(MemoryItem).where(
                MemoryItem.memory_type.in_(("semantic", "episodic", "procedural")),
                MemoryItem.valid_to.is_(None),
            )
        )
        rows = result.scalars().all()
    stats["rows"] = len(rows)
    for row in rows:
        ref = svc.add(
            row.scope_type,
            row.scope_id,
            row.content or "",
            row.memory_type,
            {"source": "postgres_seed", "memory_id": row.id},
        )
        if ref:
            stats["indexed"] += 1
        else:
            stats["errors"] += 1
        time.sleep(EMBED_PAUSE_SEC)
    return stats


def run_seed(user_scope_id: str = "default") -> Dict[str, Any]:
    reset_vector_service()
    cfg = get_vector_memory_config()
    svc = get_vector_service(cfg)
    status = svc.status()
    if not status.get("ready"):
        raise RuntimeError(f"Vector memory not ready: {status.get('error')}")

    workspace_stats = seed_workspace(user_scope_id=user_scope_id)
    postgres_stats = asyncio.run(seed_postgres_memories())
    return {
        "vector_status": status,
        "workspace": workspace_stats,
        "postgres": postgres_stats,
    }


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO)
    try:
        print(json.dumps(run_seed(), indent=2))
    except Exception as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
