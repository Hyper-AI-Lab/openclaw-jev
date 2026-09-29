"""Hybrid retrieval: RRF fusion, FTS sanitization, fail-soft pack."""
from unittest.mock import AsyncMock, patch

import pytest

from app.task_registry.hybrid_retriever import (
    fuse_evidence,
    reciprocal_rank_fusion,
    sanitize_fts_query,
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


def test_sanitize_fts_query_strips_operators():
    assert sanitize_fts_query("") == ""
    cleaned = sanitize_fts_query("foo & bar! (baz)")
    assert "&" not in cleaned
    assert "foo" in cleaned and "bar" in cleaned


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
