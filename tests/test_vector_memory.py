from unittest.mock import patch

from app.memory.mistral_embed import MistralEmbeddings
from app.memory.nvidia_embed import NvidiaEmbeddings
from app.memory.vector import VectorMemoryService, _read_mistral_key


def test_vector_service_nvidia_embedder_config():
    svc = VectorMemoryService(
        {
            "enabled": True,
            "embedder_provider": "nvidia",
            "embedder_model": "nvidia/nv-embed-v1",
            "embedding_dims": 4096,
            "collection_name": "test_memories",
        }
    )
    with patch("app.memory.vector._read_nvidia_key", return_value="test-nvidia-key"):
        cfg = svc._build_mem0_config()

    assert cfg["embedder"]["provider"] == "langchain"
    assert isinstance(cfg["embedder"]["config"]["model"], NvidiaEmbeddings)
    assert cfg["vector_store"]["config"]["embedding_model_dims"] == 4096
    assert cfg["llm"]["config"]["openai_base_url"] == "https://integrate.api.nvidia.com/v1"


def test_vector_service_qdrant_server_config():
    svc = VectorMemoryService(
        {
            "enabled": True,
            "qdrant_mode": "server",
            "qdrant_host": "127.0.0.1",
            "qdrant_port": 6333,
            "embedder_provider": "nvidia",
            "embedder_model": "nvidia/nv-embed-v1",
            "embedding_dims": 4096,
            "collection_name": "rmp_memories",
        }
    )
    with patch("app.memory.vector._read_nvidia_key", return_value="test-nvidia-key"):
        cfg = svc._build_mem0_config()

    vs = cfg["vector_store"]["config"]
    assert vs["host"] == "127.0.0.1"
    assert vs["port"] == 6333
    assert "path" not in vs
    assert vs["collection_name"] == "rmp_memories"


def test_vector_service_mistral_embedder_config():
    svc = VectorMemoryService(
        {
            "enabled": True,
            "embedder_provider": "mistral",
            "embedder_model": "mistral-embed",
            "embedding_dims": 1024,
            "collection_name": "test_memories",
        }
    )
    with patch("app.memory.vector._read_mistral_key", return_value="test-mistral-key"):
        cfg = svc._build_mem0_config()

    assert cfg["embedder"]["provider"] == "langchain"
    assert isinstance(cfg["embedder"]["config"]["model"], MistralEmbeddings)
    assert cfg["vector_store"]["config"]["embedding_model_dims"] == 1024
    assert cfg["llm"]["config"]["openai_base_url"] == "https://api.mistral.ai/v1"


def test_read_mistral_key_from_env(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "env-mistral-key")
    assert _read_mistral_key() == "env-mistral-key"
