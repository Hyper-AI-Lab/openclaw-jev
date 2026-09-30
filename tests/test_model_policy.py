import json
from pathlib import Path

from app.llm import model_policy as mp


def test_policy_shape():
    assert mp.PRIMARY_MODEL == "openai/gpt-6-luna"
    assert mp.FALLBACK_MODELS == ("nvidia/openai/gpt-oss-20b",)
    assert mp.SUBAGENT_MODEL == mp.PRIMARY_MODEL
    assert mp.GLM_MODEL not in mp.allowed_models()
    assert "deepseek" not in json.dumps(mp.allowed_models())
    assert "minimax" not in json.dumps(mp.allowed_models())
    assert "gpt-5-nano" not in json.dumps(mp.allowed_models())
    assert mp.intake_model_chain() == [
        "openai/gpt-6-luna",
        "nvidia/openai/gpt-oss-20b",
    ]
    assert (mp.THINKING_DEFAULT, mp.TASK_THINKING) == ("medium", "max")


def test_should_pin_nvidia_only_for_nvidia_models():
    # gpt-oss-20b is NVIDIA-hosted: NVIDIA key rotation applies despite the "openai/" id.
    assert mp.should_pin_nvidia_profile("nvidia/openai/gpt-oss-20b", "nvidia:key2")
    assert not mp.should_pin_nvidia_profile("openai/gpt-6-luna", "nvidia:key2")
    assert not mp.should_pin_nvidia_profile(None, "nvidia:key2")
    assert not mp.should_pin_nvidia_profile("nvidia/openai/gpt-oss-20b", "openai:default")


def test_drop_unwired_openai(monkeypatch):
    monkeypatch.setattr(mp, "openai_key_present", lambda: False)
    assert mp.drop_unwired_openai(mp.intake_model_chain()) == list(mp.FALLBACK_MODELS)
    monkeypatch.setattr(mp, "openai_key_present", lambda: True)
    assert mp.drop_unwired_openai(mp.intake_model_chain()) == mp.intake_model_chain()


def test_apply_openclaw_policy_writes_combo_and_drops_retired_models(tmp_path):
    cfg_path = tmp_path / "openclaw.json"
    cfg_path.write_text(
        json.dumps(
            {
                "auth": {
                    "profiles": {
                        "nvidia:default": {"provider": "nvidia", "mode": "api_key"}
                    },
                    "order": {"nvidia": ["nvidia:default"]},
                },
                "agents": {
                    "defaults": {
                        "model": {
                            "primary": "nvidia/minimaxai/minimax-m3",
                            "fallbacks": [
                                "nvidia/deepseek-ai/deepseek-v4-flash-0731",
                                "nvidia/z-ai/glm-5.2",
                            ],
                        },
                        "models": {
                            "nvidia/z-ai/glm-5.2": {"alias": "GLM"},
                            "nvidia/deepseek-ai/deepseek-v4-flash-0731": {"alias": "DeepSeek"},
                            "openai/gpt-5-nano": {"alias": "GPT-5 nano"},
                        },
                        "modelPolicy": {"allow": ["nvidia/z-ai/glm-5.2"]},
                        "heartbeat": {"every": "30m", "target": "none", "session": "heartbeat"},
                        "subagents": {
                            "maxConcurrent": 2,
                            "model": "nvidia/deepseek-ai/deepseek-v4-flash-0731",
                        },
                    }
                },
                "models": {
                    "providers": {
                        "openai": {
                            "api": "openai-completions",
                            "models": [{"id": "gpt-5-nano", "api": "openai-completions"}],
                        },
                        "nvidia": {
                            "models": [
                                {"id": "minimaxai/minimax-m3"},
                                {"id": "z-ai/glm-5.2"},
                                {"id": "deepseek-ai/deepseek-v4-flash-0731"},
                            ]
                        },
                    }
                },
                "plugins": {"allow": ["rmp_adapter", "nvidia"]},
            }
        )
    )
    result = mp.apply_openclaw_policy(cfg_path)
    cfg = json.loads(cfg_path.read_text())
    assert cfg["agents"]["defaults"]["model"]["primary"] == mp.PRIMARY_MODEL
    assert cfg["agents"]["defaults"]["model"]["fallbacks"] == list(mp.FALLBACK_MODELS)
    allow = cfg["agents"]["defaults"]["modelPolicy"]["allow"]
    assert allow == mp.allowed_models()
    assert "nvidia/z-ai/glm-5.2" not in json.dumps(cfg)
    assert "deepseek" not in json.dumps(cfg)
    assert cfg["agents"]["defaults"]["subagents"] == {"maxConcurrent": 2, "model": mp.PRIMARY_MODEL}
    assert cfg["agents"]["defaults"]["utilityModel"] == ""
    assert "agents.defaults.utilityModel" in result["changed"]
    runtime = cfg["agents"]["defaults"]["models"]["openai/gpt-6-luna"]["agentRuntime"]
    assert runtime == {"id": "openclaw"}
    assert cfg["agents"]["defaults"]["models"]["openai/gpt-6-luna"]["params"] == {
        "thinking": "medium"
    }
    assert cfg["agents"]["defaults"]["thinkingDefault"] == "medium"
    assert "gpt-5-nano" not in json.dumps(cfg)
    assert cfg["agents"]["defaults"]["heartbeat"] == {
        "every": "0m",
        "target": "none",
        "session": "heartbeat",
    }
    assert "agents.defaults.heartbeat.every" in result["changed"]
    openai = cfg["models"]["providers"]["openai"]
    assert openai["baseUrl"] == "https://api.openai.com/v1"
    assert openai["api"] == "openai-responses"
    assert "apiKey" not in openai
    assert [m["id"] for m in openai["models"]] == ["gpt-6-luna"]
    luna = openai["models"][0]
    assert luna["api"] == "openai-responses"
    assert luna["thinkingLevelMap"]["max"] == "max"
    assert "max" in luna["compat"]["supportedReasoningEfforts"]
    assert luna["contextWindow"] == 272000
    nvidia_ids = [m["id"] for m in cfg["models"]["providers"]["nvidia"]["models"]]
    assert "z-ai/glm-5.2" not in nvidia_ids
    assert "minimax" not in json.dumps(cfg)
    assert "openai/gpt-oss-20b" in nvidia_ids
    assert "models.providers.nvidia.models:fallback" in result["changed"]
    assert cfg["auth"]["order"]["openai"] == ["openai:default"]
    assert "openai" in cfg["plugins"]["allow"]
    assert cfg.get("memory", {}).get("search", {}).get("enabled") is False
    assert "sk-" not in cfg_path.read_text()
    assert "primary" in ",".join(result["changed"])
    # leftover JSON must not be created next to the config
    assert list(Path(cfg_path).parent.glob("auth-profiles.json*")) == []
