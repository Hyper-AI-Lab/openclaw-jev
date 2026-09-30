"""The legacy user-memory purge: only seeded workspace chunks and heuristic notes go, after a backup."""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, MemoryItem, MemoryLink
from ops import purge_legacy_user_memory as purge


@pytest.fixture
async def maker(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'purge.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(purge, "AsyncSessionLocal", maker)
    monkeypatch.setattr(purge, "RMP_DATA_DIR", str(tmp_path))
    yield maker
    await engine.dispose()


def row(mid, content, provenance=None, scope_type="user"):
    return MemoryItem(id=mid, scope_type=scope_type, scope_id="default", memory_type="semantic",
                      content=content, provenance_ref=provenance)


async def test_only_seeded_chunks_and_heuristic_notes_go_after_a_backup(maker, tmp_path, monkeypatch):
    async with maker() as db:
        db.add_all([
            row("seed", "# USER.md - About Your Human", {"source": "workspace_seed", "path": "/w/USER.md"}),
            row("site", "Site referenced: kubernetes.io (https://kubernetes.io/releases/)",
                {"kind": "environment_fact", "promotion_stage": "semantic", "task_id": "t1"}),
            row("fact", "Kirill's acceptance code word is KESTREL-58.", {"extracted_by": "gpt-6-luna"}),
            row("api", "Written through /memory/write."),
            row("proc", "Task: a procedure", {"source": "workspace_seed"}, scope_type="procedural"),
            MemoryLink(id="l1", source_id="fact", target_id="site", relation="contradicts"),
        ])
        await db.commit()
    deleted = []
    monkeypatch.setattr(purge.index, "delete_points", lambda ids: deleted.extend(ids) or len(ids))

    await purge.main(apply=False)
    assert deleted == [] and not (tmp_path / "backups").exists()

    await purge.main(apply=True)
    assert sorted(deleted) == ["seed", "site"]
    async with maker() as db:
        assert sorted((await db.execute(select(MemoryItem.id))).scalars()) == ["api", "fact", "proc"]
        assert (await db.execute(select(MemoryLink.id))).scalars().all() == []
    [backup] = (tmp_path / "backups").iterdir()
    assert backup.stat().st_mode & 0o777 == 0o600
    saved = json.loads(backup.read_text(encoding="utf-8"))
    assert sorted(r["id"] for r in saved["memory_items"]) == ["seed", "site"]
    assert [link["id"] for link in saved["memory_links"]] == ["l1"]
