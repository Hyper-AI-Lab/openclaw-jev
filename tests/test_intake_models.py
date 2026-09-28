"""Intake model chain helpers."""
from app.config import DEFAULT_TASK_REGISTRY, get_intake_models
from app.llm.model_policy import FALLBACK_MODELS, PRIMARY_MODEL


def test_intake_models_default_order():
    models = get_intake_models()
    assert DEFAULT_TASK_REGISTRY["intake_model"] == PRIMARY_MODEL
    assert DEFAULT_TASK_REGISTRY["intake_model_fallbacks"] == list(FALLBACK_MODELS)
    from app.llm.model_policy import openai_key_present

    if openai_key_present():
        assert models[0] == PRIMARY_MODEL
        assert "nvidia/openai/gpt-oss-20b" in models
        assert not any("deepseek" in m for m in models)
    else:
        assert models == [FALLBACK_MODELS[0]]
        assert not any(m.startswith("openai/") for m in models)
    assert "glm" not in "".join(models).lower()
    assert "minimax" not in "".join(models).lower()
