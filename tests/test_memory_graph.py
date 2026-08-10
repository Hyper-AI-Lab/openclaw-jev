import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.graph import link_memory, query_links, unlink_memory


def _mock_session(*, existing_link=None, source_item=True, target_item=True):
    session = AsyncMock()

    async def execute(stmt):
        result = MagicMock()
        stmt_str = str(stmt)
        if "memory_links" in stmt_str and "DELETE" not in stmt_str:
            if existing_link and "source_id" in stmt_str:
                result.scalar_one_or_none.return_value = existing_link
            else:
                scalars = MagicMock()
                if existing_link and "MemoryLink" in stmt_str:
                    scalars.all.return_value = [existing_link]
                else:
                    scalars.all.return_value = []
                result.scalars.return_value = scalars
                result.scalar_one_or_none.return_value = None
        elif "memory_items" in stmt_str:
            item = MagicMock()
            item.memory_type = "episodic"
            item.content = "peer content"
            result.scalar_one_or_none.return_value = item if source_item else None
        else:
            result.rowcount = 1
            result.scalar_one_or_none.return_value = None
        return result

    session.execute = execute
    session.add = MagicMock()
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_link_memory_creates_link():
    source_id = str(uuid.uuid4())
    target_id = str(uuid.uuid4())
    session = _mock_session()

    with patch("app.memory.graph.AsyncSessionLocal") as mock_local:
        mock_local.return_value.__aenter__.return_value = session
        link_id = await link_memory(source_id, target_id, "related_to")

    assert link_id
    session.add.assert_called_once()
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_link_memory_idempotent():
    source_id = str(uuid.uuid4())
    target_id = str(uuid.uuid4())
    existing = MagicMock()
    existing.id = "existing-link-id"
    session = _mock_session(existing_link=existing)

    with patch("app.memory.graph.AsyncSessionLocal") as mock_local:
        mock_local.return_value.__aenter__.return_value = session
        link_id = await link_memory(source_id, target_id, "related_to")

    assert link_id == "existing-link-id"
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_unlink_memory():
    source_id = str(uuid.uuid4())
    target_id = str(uuid.uuid4())
    session = AsyncMock()
    result = MagicMock()
    result.rowcount = 1
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    with patch("app.memory.graph.AsyncSessionLocal") as mock_local:
        mock_local.return_value.__aenter__.return_value = session
        removed = await unlink_memory(source_id, target_id, "related_to")

    assert removed is True


@pytest.mark.asyncio
async def test_query_links_returns_peer_info():
    memory_id = str(uuid.uuid4())
    peer_id = str(uuid.uuid4())
    link = MagicMock()
    link.id = "link-1"
    link.source_id = memory_id
    link.target_id = peer_id
    link.relation = "follows"

    session = AsyncMock()

    async def execute(stmt):
        result = MagicMock()
        stmt_str = str(stmt)
        if "memory_links" in stmt_str:
            scalars = MagicMock()
            scalars.all.return_value = [link]
            result.scalars.return_value = scalars
        elif "memory_items" in stmt_str:
            peer = MagicMock()
            peer.memory_type = "procedural"
            peer.content = "playbook step"
            result.scalar_one_or_none.return_value = peer
        return result

    session.execute = execute

    with patch("app.memory.graph.AsyncSessionLocal") as mock_local:
        mock_local.return_value.__aenter__.return_value = session
        links = await query_links(memory_id, direction="out")

    assert len(links) == 1
    assert links[0]["relation"] == "follows"
    assert links[0]["peer_id"] == peer_id
    assert links[0]["direction"] == "out"
