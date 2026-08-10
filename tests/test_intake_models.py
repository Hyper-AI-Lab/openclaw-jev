"""Intake model chain helpers."""
from app.config import DEFAULT_TASK_REGISTRY, get_intake_models


def test_intake_models_default_order():
    models = get_intake_models()
    assert models[0] == DEFAULT_TASK_REGISTRY["intake_model"]
    assert models == ["nvidia/deepseek-ai/deepseek-v4-flash-0731"]
    assert "gpt-oss" not in "".join(models)
