import pytest

from app.llm import quota_broker as qb


@pytest.fixture
def broker_env(monkeypatch, tmp_path):
    state_file = tmp_path / "llm_quota.json"
    auth_file = tmp_path / "auth-profiles.json"
    env_file = tmp_path / "openclaw.env"
    sessions_file = tmp_path / "sessions.json"
    env_file.write_text(
        "NVIDIA_API_KEY=k1\nNVIDIA_API_KEY_2=k2\nNVIDIA_API_KEY_3=k3\n"
    )
    auth_file.write_text('{"version":1,"profiles":{},"usageStats":{}}')
    sessions_file.write_text("{}")
    monkeypatch.setattr(qb, "STATE_PATH", state_file)
    monkeypatch.setattr(qb, "AUTH_PROFILES_PATH", auth_file)
    monkeypatch.setattr(qb, "OPENCLAW_ENV_PATH", env_file)
    monkeypatch.setattr(qb, "_today_usage_by_profile", lambda: {})
    monkeypatch.setattr(
        qb,
        "assign_openclaw_session_profile",
        lambda session_key, profile_id: None,
    )
    return state_file


@pytest.mark.asyncio
async def test_reserve_respects_max_concurrent(broker_env):
    settings = {"llm_quota": {"max_concurrent": 2, "min_interval_sec": 0, "max_wait_sec": 5}}

    p1, s1 = await qb.reserve_profile(session_key="agent:main:one", settings=settings)
    p2, s2 = await qb.reserve_profile(session_key="agent:main:two", settings=settings)
    assert p1 and p2

    status = qb.get_orchestration_status(settings)
    assert status["active_slots"] == 2

    await qb.release_profile(session_key="agent:main:one")
    p3, s3 = await qb.reserve_profile(session_key="agent:main:three", settings=settings)
    assert p3

    await qb.release_profile(session_key="agent:main:two")
    await qb.release_profile(session_key="agent:main:three")


@pytest.mark.asyncio
async def test_reserve_idempotent_per_session(broker_env):
    settings = {"llm_quota": {"max_concurrent": 2, "min_interval_sec": 0, "max_wait_sec": 5}}

    p1, s1 = await qb.reserve_profile(session_key="agent:main:main", settings=settings)
    p2, s2 = await qb.reserve_profile(session_key="agent:main:main", settings=settings)
    assert p1 == p2
    assert s1 == s2
    assert qb.get_orchestration_status(settings)["active_slots"] == 1

    await qb.release_profile(session_key="agent:main:main")
