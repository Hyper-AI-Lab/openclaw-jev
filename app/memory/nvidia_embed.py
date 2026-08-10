"""LangChain-compatible NVIDIA NIM embeddings for Mem0."""
import threading
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from langchain_core.embeddings import Embeddings
from openai import OpenAI

from app.llm.quota_broker import (
    api_key_for_profile,
    is_rate_limit_message,
    record_rate_limit,
    record_success,
    wait_for_dispatch_sync,
)
from app.llm.usage_monitor import estimate_tokens, record_request

NVIDIA_API_BASE = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "nvidia/nv-embed-v1"

# Short-lived cache so parallel memory-scope searches for the same query
# share one NVIDIA embed call instead of N serial embeds.
_EMBED_CACHE: "OrderedDict[str, Tuple[float, List[float]]]" = OrderedDict()
_EMBED_CACHE_LOCK = threading.Lock()
_EMBED_CACHE_TTL_SEC = 60.0
_EMBED_CACHE_MAX = 64
_INFLIGHT_LOCKS: Dict[str, threading.Lock] = {}
_INFLIGHT_GUARD = threading.Lock()


def _cache_key(model: str, text: str) -> str:
    return f"{model}\0{text}"


def _inflight_lock(key: str) -> threading.Lock:
    with _INFLIGHT_GUARD:
        lock = _INFLIGHT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _INFLIGHT_LOCKS[key] = lock
        return lock


def _cache_get(model: str, text: str) -> Optional[List[float]]:
    key = _cache_key(model, text)
    now = time.monotonic()
    with _EMBED_CACHE_LOCK:
        item = _EMBED_CACHE.get(key)
        if not item:
            return None
        ts, vec = item
        if now - ts > _EMBED_CACHE_TTL_SEC:
            _EMBED_CACHE.pop(key, None)
            return None
        _EMBED_CACHE.move_to_end(key)
        return list(vec)


def _cache_put(model: str, text: str, vec: List[float]) -> None:
    key = _cache_key(model, text)
    with _EMBED_CACHE_LOCK:
        _EMBED_CACHE[key] = (time.monotonic(), list(vec))
        _EMBED_CACHE.move_to_end(key)
        while len(_EMBED_CACHE) > _EMBED_CACHE_MAX:
            _EMBED_CACHE.popitem(last=False)


def clear_embed_cache() -> None:
    with _EMBED_CACHE_LOCK:
        _EMBED_CACHE.clear()
    with _INFLIGHT_GUARD:
        _INFLIGHT_LOCKS.clear()


class NvidiaEmbeddings(Embeddings):
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        self.model = model
        self._default_api_key = api_key
        self.client = OpenAI(api_key=api_key, base_url=NVIDIA_API_BASE)

    def _api_key_for_profile(self, profile_id: str) -> str:
        return api_key_for_profile(profile_id)

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text: str) -> List[float]:
        text = text.replace("\n", " ")
        cached = _cache_get(self.model, text)
        if cached is not None:
            return cached

        # Singleflight: parallel scope searches for the same query share one HTTP call.
        key = _cache_key(self.model, text)
        with _inflight_lock(key):
            cached = _cache_get(self.model, text)
            if cached is not None:
                return cached

            profile_id: Optional[str] = None
            last_error: Optional[Exception] = None

            for attempt in range(6):
                profile_id = wait_for_dispatch_sync()
                api_key = self._api_key_for_profile(profile_id)
                client = (
                    self.client
                    if api_key == self._default_api_key
                    else OpenAI(api_key=api_key, base_url=NVIDIA_API_BASE)
                )
                try:
                    response = client.embeddings.create(input=[text], model=self.model)
                    record_success(profile_id)
                    est = estimate_tokens(text)
                    usage = getattr(response, "usage", None)
                    total_t = (
                        int(getattr(usage, "total_tokens", 0) or est) if usage else est
                    )
                    record_request(
                        profile_id,
                        "embed",
                        input_tokens=est,
                        total_tokens=total_t,
                        model=self.model,
                    )
                    vec = response.data[0].embedding
                    _cache_put(self.model, text, vec)
                    return vec
                except Exception as exc:
                    last_error = exc
                    if is_rate_limit_message(str(exc)):
                        record_rate_limit(profile_id, source="embed")
                        continue
                    raise

            raise RuntimeError(
                f"NVIDIA embedding failed after retries: {last_error}"
            ) from last_error
