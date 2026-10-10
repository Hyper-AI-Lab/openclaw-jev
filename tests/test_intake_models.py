"""Intake model chain helpers."""
import pytest

from app.config import DEFAULT_TASK_REGISTRY, get_intake_models
from app.llm.model_policy import FALLBACK_MODELS, PRIMARY_MODEL


@pytest.mark.parametrize("openai_key", [True, False], ids=["openai-key-present", "openai-key-absent"])
def test_intake_models_default_order(openai_key, monkeypatch):
    if openai_key:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    else:
        # The OpenClaw env file is the conftest's absent default, so nothing else supplies the key.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    models = get_intake_models()
    assert DEFAULT_TASK_REGISTRY["intake_model"] == PRIMARY_MODEL
    assert DEFAULT_TASK_REGISTRY["intake_model_fallbacks"] == list(FALLBACK_MODELS)
    if openai_key:
        assert models[0] == PRIMARY_MODEL
        assert "nvidia/openai/gpt-oss-20b" in models
        assert not any("deepseek" in m for m in models)
    else:
        assert models == [FALLBACK_MODELS[0]]
        assert not any(m.startswith("openai/") for m in models)
    assert "glm" not in "".join(models).lower()
    assert "minimax" not in "".join(models).lower()
