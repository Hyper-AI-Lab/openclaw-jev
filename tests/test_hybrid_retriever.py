"""Hybrid retrieval: RRF fusion, FTS sanitization, fail-soft pack."""
from unittest.mock import AsyncMock, patch

import pytest

from app.task_registry import hybrid_retriever
from app.task_registry.hybrid_retriever import (
    fuse_evidence,
    or_query,
    reciprocal_rank_fusion,
    status_boost,
)


def test_rrf_prefers_docs_in_both_lists():
    scores = reciprocal_rank_fusion(
        [
            ["exact-token", "other"],
            ["paraphrase", "exact-token"],
        ]
    )
    assert scores["exact-token"] > scores["other"]
    assert scores["exact-token"] > scores["paraphrase"]


def test_status_boost_active_over_old_completed():
    assert status_boost("running") > status_boost("completed", age_days=40)
    assert status_boost("blocked") > status_boost("completed", age_days=40)
    assert status_boost("completed", age_days=2) > status_boost("failed")


def test_the_text_query_asks_for_any_word_without_operators():
    assert or_query("") == "" and or_query("& ! ()") == ""
    assert or_query("Foo & bar! (baz) -qux foo to") == "foo or bar or baz or qux"
    assert or_query(" ".join(f"word{i}" for i in range(30))).count(" or ") == 15


def test_fuse_rrf_exact_token_and_dense_paraphrase():
    ranked = fuse_evidence(
        active=[],
        fts=[
            {
                "citation": "task:aaa",
                "task_id": "aaa",
                "snippet": "SKU-7842-XL order",
                "status": "completed",
            }
        ],
        dense=[
            {
                "citation": "task:bbb",
                "task_id": "bbb",
                "snippet": "similar shopping request",
                "status": "completed",
            },
            {
                "citation": "task:aaa",
                "task_id": "aaa",
                "snippet": "SKU-7842-XL order",
                "status": "completed",
            },
        ],
        memory=[],
        limit=5,
    )
    assert ranked[0]["task_id"] == "aaa"
    assert "fts" in ranked[0]["sources"] and "dense" in ranked[0]["sources"]


@pytest.mark.asyncio
async def test_assemble_evidence_pack_fail_soft_on_fts_and_memory():
    from app.task_registry.hybrid_retriever import assemble_evidence_pack

    with patch(
        "app.task_registry.hybrid_retriever.search_fts",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        with patch(
            "app.task_registry.hybrid_retriever.search_user_memory",
            new=AsyncMock(return_value=[]),
        ):
            # search_fts is awaited via create_task; exception would break gather
            pass

    with patch(
        "app.task_registry.hybrid_retriever.search_fts",
        new=AsyncMock(return_value=[]),
    ):
        with patch(
            "app.task_registry.hybrid_retriever.search_user_memory",
            new=AsyncMock(return_value=[{"citation": "memory:m1", "snippet": "past fact", "source": "memory"}]),
        ):
            pack = await assemble_evidence_pack(
                "hello",
                active=[{"task_id": "t1", "status": "running", "goal": "do work", "task_type": "user"}],
                recent=[],
                dense=[],
                include_liveness=False,
            )
    assert pack["memory_hits"]
    assert any(r.get("task_id") == "t1" for r in pack["ranked"])


@pytest.mark.asyncio
async def test_the_incoming_requests_own_reservation_is_not_evidence():
    """The reservation row carries the new message as its goal; showing it made intake ask
    Kirill whether he wanted a duplicate of his own request (Sep 30 acceptance DM)."""
    from app.task_registry.hybrid_retriever import search_fts

    statements = []

    class _Rows:
        def mappings(self):
            return self

        def all(self):
            return []

    class _Db:
        async def execute(self, stmt, params):
            statements.append(str(stmt))
            return _Rows()

    await search_fts("Remember this for later: my test code word", db=_Db())
    [tasks_sql] = [s for s in statements if "FROM tasks," in s]
    assert "coalesce(supplementary_context->>'intake_reserved', 'false') <> 'true'" in tasks_sql


@pytest.mark.asyncio
async def test_text_search_matches_any_word_of_recent_user_work_only():
    from datetime import datetime, timedelta

    from app.task_registry.hybrid_retriever import search_fts

    calls = []

    class _Rows:
        def __init__(self, rows):
            self.rows = rows

        def mappings(self):
            return self

        def all(self):
            return self.rows

    class _Db:
        async def execute(self, stmt, params):
            calls.append((str(stmt), params))
            if "FROM task_messages" in str(stmt):
                return _Rows([{"task_id": "m1", "snippet": "RMP CANARY: reply with CANARY_OK", "task_type": "user"},
                              {"task_id": "m2", "snippet": "The code word is PELICAN-47", "task_type": "user"}])
            return _Rows([])

    hits = await search_fts("What's my test code word?", db=_Db())
    assert [h["task_id"] for h in hits] == ["m2"]
    assert len(calls) == 3
    for sql, params in calls:
        assert "websearch_to_tsquery('english', :q)" in sql and "plainto_tsquery" not in sql
        assert "NOT IN ('canary', 'heartbeat')" in sql and ">= :since" in sql
        assert params["q"] == "what or test or code or word"
        assert timedelta(days=89) < datetime.utcnow() - params["since"] < timedelta(days=91)


@pytest.mark.asyncio
async def test_a_slow_memory_search_costs_only_its_own_leg(monkeypatch):
    import asyncio

    from app.task_registry.hybrid_retriever import assemble_evidence_pack

    async def text_hits(query, limit):
        return [{"citation": "task:t9", "task_id": "t9", "snippet": "code word PELICAN-47", "source": "fts_message"}]

    async def slow_memory(query, limit):
        await asyncio.sleep(5)
        return [{"citation": "memory:m1", "snippet": "never arrives", "source": "memory"}]

    monkeypatch.setattr(hybrid_retriever, "search_fts", text_hits)
    monkeypatch.setattr(hybrid_retriever, "search_user_memory", slow_memory)
    monkeypatch.setattr(hybrid_retriever, "MEMORY_DEADLINE_SEC", 0.1)
    pack = await asyncio.wait_for(assemble_evidence_pack("code word", include_liveness=False), timeout=2)
    assert [h["task_id"] for h in pack["fts_hits"]] == ["t9"] and pack["memory_hits"] == []
    assert [r["task_id"] for r in pack["ranked"]] == ["t9"]
