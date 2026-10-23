"""Task registry vector store — query_points API."""
from unittest.mock import MagicMock, patch

import pytest

from app.task_registry import vector_store

SERVER = {"qdrant_mode": "server", "qdrant_host": "127.0.0.1", "qdrant_port": 6333, "qdrant_grpc_port": 6334}


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

    with patch.object(vector_store, "is_vector_memory_enabled", return_value=True):
        with patch.object(
            vector_store,
            "get_task_registry_config",
            return_value={"enabled": True, "collection_name": "rmp_task_registry"},
        ):
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


def test_the_registry_client_sends_the_key_over_http(monkeypatch):
    """With QDRANT_API_KEY the client sends it in the api-key header, on REST and on gRPC, and stays on plain HTTP: a key
    alone would turn qdrant_client to HTTPS, which the local server does not speak. No request is made."""
    monkeypatch.setattr(vector_store, "get_vector_memory_config", lambda: SERVER)
    monkeypatch.setattr(vector_store, "_grpc_port_open", lambda host, port: (host, port) == ("127.0.0.1", 6334))
    monkeypatch.setattr(vector_store, "_client", None)
    monkeypatch.setenv("QDRANT_API_KEY", "k")
    with pytest.warns(UserWarning, match="Api key is used with an insecure connection"):
        client = vector_store._get_qdrant_client()
    remote = client._client
    assert remote.rest_uri == "http://127.0.0.1:6333" and remote._scheme == "http" and not remote._https
    assert remote._rest_headers["api-key"] == "k" and ("api-key", "k") in remote._grpc_headers
    assert remote._prefer_grpc and remote._grpc_port == 6334

    monkeypatch.setattr(vector_store, "_client", None)
    monkeypatch.setenv("QDRANT_API_KEY", "")
    remote = vector_store._get_qdrant_client()._client
    assert remote.rest_uri == "http://127.0.0.1:6333" and "api-key" not in remote._rest_headers
    assert not any(name == "api-key" for name, _ in remote._grpc_headers)
