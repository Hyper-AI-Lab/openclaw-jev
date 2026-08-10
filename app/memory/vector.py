"""Mem0 + Qdrant vector memory backend for semantic/episodic recall."""
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from app.memory.mistral_embed import MISTRAL_API_BASE, MistralEmbeddings
from app.memory.nvidia_embed import NVIDIA_API_BASE, NvidiaEmbeddings
from app.memory.policy import redact_secrets

logger = logging.getLogger("rmp.vector_memory")

OPENCLAW_AUTH_PATH = "/root/.openclaw/agents/main/agent/auth-profiles.json"
OPENCLAW_ENV_PATH = "/etc/openclaw/openclaw.env"
DEFAULT_QDRANT_PATH = "/root/.openclaw/rmp/data/qdrant"
DEFAULT_COLLECTION = "rmp_memories"

_service: Optional["VectorMemoryService"] = None
_lock = threading.Lock()

INDEXABLE_TYPES = frozenset({"semantic", "episodic", "procedural", "pinned"})


def _load_env_file(path: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def _read_profile_key(provider: str) -> str:
    try:
        with open(OPENCLAW_AUTH_PATH, "r", encoding="utf-8") as handle:
            auth = json.load(handle)
        for profile in auth.get("profiles", {}).values():
            if profile.get("provider") == provider and profile.get("key"):
                return profile["key"]
    except Exception as exc:
        logger.warning("Could not read %s key from auth profiles: %s", provider, exc)
    return ""


def _read_api_key(env_var: str, provider: Optional[str] = None) -> str:
    key = os.environ.get(env_var, "").strip()
    if key:
        return key
    file_values = _load_env_file(OPENCLAW_ENV_PATH)
    key = file_values.get(env_var, "").strip()
    if key:
        return key
    if provider:
        return _read_profile_key(provider)
    return ""


def _read_nvidia_key() -> str:
    return _read_api_key("NVIDIA_API_KEY", provider="nvidia")


def _read_mistral_key() -> str:
    return _read_api_key("MISTRAL_API_KEY", provider="mistral")


def _read_openai_key() -> str:
    return _read_api_key("OPENAI_API_KEY", provider="openai")


class VectorMemoryService:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._memory = None
        self._ready = False
        self._error: Optional[str] = None

    def _qdrant_vector_store_config(self, embedding_dims: int) -> Dict[str, Any]:
        mode = (self.config.get("qdrant_mode") or "embedded").strip().lower()
        collection = self.config.get("collection_name", DEFAULT_COLLECTION)
        base: Dict[str, Any] = {
            "collection_name": collection,
            "embedding_model_dims": embedding_dims,
            "on_disk": True,
        }
        if mode == "server" or self.config.get("qdrant_host"):
            base["host"] = self.config.get("qdrant_host", "127.0.0.1")
            base["port"] = int(self.config.get("qdrant_port", 6333))
            return base
        qdrant_path = self.config.get("qdrant_path", DEFAULT_QDRANT_PATH)
        os.makedirs(qdrant_path, exist_ok=True)
        base["path"] = qdrant_path
        return base

    def _build_mem0_config(self) -> Dict[str, Any]:
        provider = self.config.get("embedder_provider", "openai")
        embedder_model = self.config.get("embedder_model", "text-embedding-3-small")
        embedding_dims = int(self.config.get("embedding_dims", 1536))

        if provider == "mistral":
            api_key = _read_mistral_key()
            if not api_key:
                raise RuntimeError("MISTRAL_API_KEY required for vector embeddings")
            embedder_model = self.config.get("embedder_model", "mistral-embed")
            embedding_dims = int(self.config.get("embedding_dims", 1024))
            embedder = {
                "provider": "langchain",
                "config": {
                    "model": MistralEmbeddings(api_key=api_key, model=embedder_model),
                },
            }
            llm = {
                "provider": "openai",
                "config": {
                    "api_key": api_key,
                    "model": "mistral-small-latest",
                    "openai_base_url": MISTRAL_API_BASE,
                },
            }
        elif provider == "nvidia":
            api_key = _read_nvidia_key()
            if not api_key:
                raise RuntimeError("NVIDIA_API_KEY required for vector embeddings")
            embedder_model = self.config.get(
                "embedder_model", "nvidia/nv-embed-v1"
            )
            embedding_dims = int(self.config.get("embedding_dims", 4096))
            embedder = {
                "provider": "langchain",
                "config": {
                    "model": NvidiaEmbeddings(api_key=api_key, model=embedder_model),
                },
            }
            llm = {
                "provider": "openai",
                "config": {
                    "api_key": api_key,
                    "model": "minimaxai/minimax-m3",
                    "openai_base_url": NVIDIA_API_BASE,
                },
            }
        else:
            api_key = _read_openai_key()
            if not api_key:
                raise RuntimeError("OpenAI API key required for vector embeddings")
            embedder = {
                "provider": "openai",
                "config": {
                    "api_key": api_key,
                    "model": embedder_model,
                    "embedding_dims": embedding_dims,
                },
            }
            llm = {
                "provider": "openai",
                "config": {"api_key": api_key, "model": "gpt-4o-mini"},
            }

        return {
            "vector_store": {
                "provider": "qdrant",
                "config": self._qdrant_vector_store_config(embedding_dims),
            },
            "embedder": embedder,
            "llm": llm,
        }

    def _ensure_client(self) -> bool:
        if self._ready:
            return True
        if self._error:
            return False
        try:
            from mem0 import Memory

            self._memory = Memory.from_config(self._build_mem0_config())
            self._ready = True
            logger.info("Vector memory (Mem0/Qdrant) initialized")
            return True
        except Exception as e:
            self._error = str(e)
            logger.warning("Vector memory unavailable: %s", e)
            return False

    def status(self) -> Dict[str, Any]:
        if self.config.get("enabled", True) and not self._ready and not self._error:
            self._ensure_client()
        mode = (self.config.get("qdrant_mode") or "embedded").strip().lower()
        out: Dict[str, Any] = {
            "enabled": bool(self.config.get("enabled", True)),
            "ready": self._ready,
            "error": self._error,
            "qdrant_mode": mode,
            "collection": self.config.get("collection_name", DEFAULT_COLLECTION),
            "embedder_provider": self.config.get("embedder_provider", "openai"),
            "embedder_model": self.config.get("embedder_model"),
            "embedding_dims": self.config.get("embedding_dims"),
        }
        if mode == "server" or self.config.get("qdrant_host"):
            out["qdrant_host"] = self.config.get("qdrant_host", "127.0.0.1")
            out["qdrant_port"] = int(self.config.get("qdrant_port", 6333))
        else:
            out["qdrant_path"] = self.config.get("qdrant_path", DEFAULT_QDRANT_PATH)
        return out

    def add(
        self,
        scope_type: str,
        scope_id: str,
        content: str,
        memory_type: str,
        provenance: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        if not self.config.get("enabled", True):
            return None
        if memory_type not in INDEXABLE_TYPES:
            return None
        if not content or not content.strip():
            return None
        if not self._ensure_client():
            return None

        ids = scope_to_mem0_ids(scope_type, scope_id)
        metadata: Dict[str, Any] = {
            "memory_type": memory_type,
            "scope_type": scope_type,
            "scope_id": scope_id,
        }
        if provenance:
            metadata["provenance"] = provenance
        if ids.get("scope_id"):
            metadata["procedural_scope_id"] = ids["scope_id"]

        mem0_kwargs = {k: v for k, v in ids.items() if k in ("user_id", "agent_id", "run_id")}

        try:
            result = self._memory.add(
                content.strip(),
                infer=False,
                metadata=metadata,
                **mem0_kwargs,
            )
            items = result.get("results") if isinstance(result, dict) else None
            if items:
                return items[0].get("id")
            return None
        except Exception as e:
            logger.warning("Vector memory add failed: %s", e)
            return None

    def search(
        self,
        scope_type: str,
        scope_id: str,
        query: str,
        limit: int = 5,
        memory_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not self.config.get("enabled", True):
            return []
        if not query or not query.strip():
            return []
        if not self._ensure_client():
            return []

        ids = scope_to_mem0_ids(scope_type, scope_id)
        filters: Dict[str, Any] = {}
        if memory_type:
            filters["memory_type"] = memory_type

        try:
            kwargs = {k: v for k, v in ids.items() if k in ("user_id", "agent_id", "run_id")}
            result = self._memory.search(
                query.strip(),
                limit=limit,
                filters=filters or None,
                **kwargs,
            )
            hits = result.get("results", []) if isinstance(result, dict) else []
            return [
                {
                    "id": h.get("id"),
                    "memory_type": (h.get("metadata") or {}).get("memory_type", "semantic"),
                    "content": redact_secrets(h.get("memory", "")),
                    "confidence": int((h.get("score") or 0) * 100),
                    "score": h.get("score"),
                    "source": "vector",
                }
                for h in hits
                if h.get("memory")
            ]
        except Exception as e:
            logger.warning("Vector memory search failed: %s", e)
            return []

    def delete(self, memory_id: str) -> bool:
        if not memory_id or not self._ensure_client():
            return False
        try:
            self._memory.delete(memory_id)
            return True
        except Exception as e:
            logger.warning("Vector memory delete failed for %s: %s", memory_id, e)
            return False


def scope_to_mem0_ids(scope_type: str, scope_id: str) -> Dict[str, Any]:
    """Map RMP memory scopes to Mem0 session identifiers."""
    if scope_type == "user":
        return {"user_id": scope_id}
    if scope_type == "process":
        return {"run_id": scope_id}
    if scope_type == "task":
        return {"run_id": f"task:{scope_id}"}
    if scope_type == "procedural":
        return {"agent_id": "procedural", "scope_id": scope_id}
    return {"run_id": scope_id}


def get_vector_service(config: Optional[Dict[str, Any]] = None) -> VectorMemoryService:
    global _service
    from app.config import get_vector_memory_config

    cfg = config or get_vector_memory_config()
    with _lock:
        if _service is None or _service.config != cfg:
            _service = VectorMemoryService(cfg)
        return _service


def reset_vector_service() -> None:
    global _service
    with _lock:
        _service = None
