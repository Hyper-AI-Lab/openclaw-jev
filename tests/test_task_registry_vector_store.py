"""Task registry vector store — query_points API."""
from unittest.mock import MagicMock, patch

from app.task_registry import vector_store


def test_normalize_query_hits_filters_by_score():
    hit_ok = MagicMock(score=0.9, payload={"task_id": "t1", "intent_snippet": "hello"})
    hit_low = MagicMock(score=0.1, payload={"task_id": "t2"})
    response = MagicMock(points=[hit_ok, hit_low])
    out = vector_store._normalize_query_hits(response, min_score=0.5)
    assert len(out) == 1
    assert out[0]["task_id"] == "t1"
    assert out[0]["score"] == 0.9


def test_search_similar_tasks_uses_query_points():
    mock_client = MagicMock()
    point = MagicMock(
        score=0.88,
        payload={
            "task_id": "abc",
            "terminal_status": "completed",
            "process_type": "user",
            "recurrence_key": None,
            "session_key": "agent:main:main",
            "intent_snippet": "hello",
        },
    )
    mock_client.collection_exists.return_value = True
    mock_client.query_points.return_value = MagicMock(points=[point])

    with patch.object(vector_store, "_get_qdrant_client", return_value=mock_client):
        with patch.object(vector_store, "_embed", return_value=[0.1, 0.2]):
            hits = vector_store.search_similar_tasks("hello aura", limit=3, min_score=0.5)

    mock_client.query_points.assert_called_once()
    call_kw = mock_client.query_points.call_args.kwargs
    assert call_kw["collection_name"] == "rmp_task_registry"
    assert call_kw["limit"] == 3
    assert "timeout" in call_kw
    assert len(hits) == 1
    assert hits[0]["task_id"] == "abc"
