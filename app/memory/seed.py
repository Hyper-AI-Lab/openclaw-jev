"""Seed memory from workspace markdown into Postgres; the vector outbox indexes it."""
import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from sqlalchemy import select

from app.config import OPENCLAW_HOME, get_vector_memory_config
from app.db.database import AsyncSessionLocal
from app.db.models import MemoryItem
from app.memory.vector import get_vector_service, reset_vector_service

logger = logging.getLogger("rmp.vector_seed")

WORKSPACE_ROOT = Path(OPENCLAW_HOME) / "workspace"
WORKSPACE_FILES = ("USER.md", "MEMORY.md", "SOUL.md", "AGENTS.md")
MIN_CHUNK_CHARS = 80
MAX_CHUNK_CHARS = 1800


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


async def _stored(scope_type: str, scope_id: str, content: str) -> bool:
    async with AsyncSessionLocal() as db:
        row = await db.execute(
            select(MemoryItem.id).where(
                MemoryItem.scope_type == scope_type,
                MemoryItem.scope_id == scope_id,
                MemoryItem.content == content,
                MemoryItem.valid_to.is_(None),
            ).limit(1)
        )
        return row.scalar_one_or_none() is not None


async def seed_workspace(user_scope_id: str = "default") -> Dict[str, Any]:
    """Workspace chunks become memory rows (skipping ones already stored)."""
    from app.memory.router import MemoryRouter

    stats = {"files": 0, "chunks": 0, "stored": 0, "already_stored": 0, "errors": 0}
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
            if await _stored(scope_type, user_scope_id, chunk):
                stats["already_stored"] += 1
                continue
            try:
                await MemoryRouter.write(
                    scope_type=scope_type,
                    scope_id=user_scope_id,
                    memory_type="semantic",
                    content=chunk,
                    provenance={"source": "workspace_seed", "path": str(path)},
                )
                stats["stored"] += 1
            except ValueError as exc:
                logger.warning("Workspace chunk from %s rejected: %s", path, exc)
                stats["errors"] += 1
    return stats


async def run_seed(user_scope_id: str = "default") -> Dict[str, Any]:
    from app.memory.vector_sync import drain_once, reconcile

    reset_vector_service()
    status = get_vector_service(get_vector_memory_config()).status()
    if not status.get("ready"):
        raise RuntimeError(f"Vector memory not ready: {status.get('error')}")

    workspace_stats = await seed_workspace(user_scope_id=user_scope_id)
    reconcile_stats = await reconcile(apply=True)
    indexed = 0
    while True:
        drained = await drain_once(limit=100)
        indexed += drained["done"]
        if not drained["done"]:
            break
    return {
        "vector_status": status,
        "workspace": workspace_stats,
        "reconcile": reconcile_stats,
        "indexed": indexed,
    }


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO)
    try:
        print(json.dumps(asyncio.run(run_seed()), indent=2, default=str))
    except Exception as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
