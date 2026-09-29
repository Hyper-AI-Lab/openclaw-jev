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
    settings = {"llm_quota": {"max_concurrent": 3, "min_interval_sec": 0, "max_wait_sec": 5}}

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


FULL = {"llm_quota": {"max_concurrent": 3, "min_interval_sec": 0, "max_wait_sec": 1800}}


@pytest.fixture
def full_broker(broker_env, monkeypatch):
    """Both user slots taken (max_concurrent 3 = 2 user + 1 canary)."""
    monkeypatch.setattr(qb, "reap_stale_llm_slots_sync", lambda **_: [])
    monkeypatch.setattr(qb, "_patch_auth_last_good", lambda profile_id: None)

    async def _fill():
        await qb.reserve_profile(session_key="agent:main:one", settings=FULL)
        await qb.reserve_profile(session_key="agent:main:two", settings=FULL)

    return _fill


@pytest.mark.asyncio
async def test_release_is_not_blocked_by_a_waiting_reserve(full_broker):
    import asyncio

    await full_broker()
    waiter = asyncio.create_task(
        qb.reserve_profile(session_key="agent:main:three", settings=FULL)
    )
    await asyncio.sleep(0.3)
    assert not waiter.done()

    await asyncio.wait_for(qb.release_profile(session_key="agent:main:one"), timeout=1.0)
    profile_id, _slot = await asyncio.wait_for(waiter, timeout=3.0)
    assert profile_id


@pytest.mark.asyncio
async def test_reserve_gives_up_at_the_callers_deadline(full_broker, caplog):
    import logging
    import time

    await full_broker()
    t0 = time.time()
    with caplog.at_level(logging.WARNING, logger="rmp.llm_quota"):
        with pytest.raises(TimeoutError, match="user slots full"):
            await qb.reserve_profile(
                session_key="agent:main:three", settings=FULL, deadline=t0 + 1.0
            )
    assert time.time() - t0 < 2.0
    assert "LLM reserve gave up after" in caplog.text
    assert "user slots full (2/2, 2/3 total)" in caplog.text


@pytest.mark.asyncio
async def test_reserve_logs_a_long_wait_with_its_reason(full_broker, caplog):
    import asyncio
    import logging

    await full_broker()

    async def _release_later():
        await asyncio.sleep(2.3)
        await qb.release_profile(session_key="agent:main:two")

    releaser = asyncio.create_task(_release_later())
    with caplog.at_level(logging.INFO, logger="rmp.llm_quota"):
        profile_id, _slot = await qb.reserve_profile(
            session_key="agent:main:three", settings=FULL
        )
    await releaser
    assert profile_id
    assert "LLM reserve waited" in caplog.text
    assert "last_reason=user slots full" in caplog.text


def test_mutate_reserve_says_why_it_failed(broker_env, monkeypatch):
    import time

    monkeypatch.setattr(qb, "_load_env_keys", lambda: [("nvidia:default", "k1")])
    cfg = qb.QuotaConfig(min_interval_sec=60, max_concurrent=3)
    assert qb._mutate_reserve("agent:main:a", ["nvidia:default"], cfg)
    why = []
    assert qb._mutate_reserve("agent:main:b", ["nvidia:default"], cfg, why=why) is None
    assert why and why[-1].startswith("nvidia:default paced (")

    until = time.time() * 1000 + 60_000
    qb._mutate_state(
        lambda s: s.setdefault("keys", {}).setdefault("nvidia:default", {}).update(
            {"cooldown_until_ms": until}
        )
    )
    why.clear()
    assert qb._mutate_reserve("agent:main:c", ["nvidia:default"], cfg, why=why) is None
    assert why == ["every key is cooling down"]


class _Info:
    def __init__(self, started, timeout_sec):
        from datetime import timedelta

        self.started_time = started
        self.start_to_close_timeout = timedelta(seconds=timeout_sec)


@pytest.mark.asyncio
async def test_intake_and_evaluator_bound_the_quota_wait_by_their_activity(monkeypatch):
    from datetime import datetime, timezone

    from app.activities import openclaw_activities as oa

    started = datetime.now(timezone.utc)
    monkeypatch.setattr(oa.activity, "info", lambda: _Info(started, 70))
    monkeypatch.setattr("app.llm.model_policy.openai_key_present", lambda: True)
    seen = []

    async def reserve_times_out(**kwargs):
        seen.append(kwargs.get("deadline"))
        raise TimeoutError("LLM quota: no slot/profile available within 1s (user slots full)")

    monkeypatch.setattr(oa, "reserve_profile", reserve_times_out)
    expected = started.timestamp() + 70 - oa.ACTIVITY_WRAP_UP_SEC

    out = await oa._execute_on_internal_session("t-1", "judge this")
    assert out.startswith("Error: LLM quota")
    # Both evaluator models share the one activity deadline.
    assert seen == [pytest.approx(expected), pytest.approx(expected)]

    seen.clear()
    with pytest.raises(TimeoutError):
        await oa._execute_intake_llm("fp123", "classify this")
    assert seen and seen[0] == pytest.approx(expected)


VERDICT = '{"verdict": "accept", "quality": "pass", "reason": "answers the question"}'


def _evaluator_dispatch(monkeypatch, replies):
    """Stub the gateway turn: each call pops the next reply (an Exception is raised)."""
    from app.activities import openclaw_activities as oa

    monkeypatch.setattr("app.llm.model_policy.openai_key_present", lambda: True)
    calls = []

    async def dispatch(session_key, message, **kwargs):
        calls.append((session_key, kwargs.get("model")))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(oa, "_dispatch_openclaw_session", dispatch)
    return oa, calls


@pytest.mark.asyncio
async def test_evaluator_walks_to_the_fallback_with_an_explicit_model(monkeypatch):
    from app.activities.openclaw_activities import OpenClawError
    from app.llm.model_policy import FALLBACK_MODELS, SUBAGENT_MODEL

    oa, calls = _evaluator_dispatch(
        monkeypatch, [OpenClawError("LLM request timed out."), VERDICT]
    )
    out = await oa._execute_on_internal_session("t-9", "judge this")
    assert out == VERDICT
    assert calls == [
        ("agent:main:rmp_verify_t-9", SUBAGENT_MODEL),
        ("agent:main:rmp_verify_t-9_fb1", FALLBACK_MODELS[0]),
    ]


@pytest.mark.asyncio
async def test_evaluator_moves_on_from_an_unparseable_verdict(monkeypatch):
    oa, calls = _evaluator_dispatch(monkeypatch, ["I think it is fine.", VERDICT])
    assert await oa._execute_on_internal_session("t-8", "judge this") == VERDICT
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_evaluator_first_good_verdict_skips_the_fallback(monkeypatch):
    oa, calls = _evaluator_dispatch(monkeypatch, [VERDICT])
    assert await oa._execute_on_internal_session("t-7", "judge this") == VERDICT
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_evaluator_returns_the_last_error_when_every_model_fails(monkeypatch):
    from app.orchestrator.process_evaluator import parse_evaluator_response

    oa, calls = _evaluator_dispatch(
        monkeypatch, [TimeoutError("LLM quota: no slot"), TimeoutError("LLM quota: still no slot")]
    )
    out = await oa._execute_on_internal_session("t-6", "judge this")
    assert out == "Error: LLM quota: still no slot"
    assert parse_evaluator_response(out)["parse_error"] is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_user_task_runs_think_at_max_and_internal_runs_at_the_default(monkeypatch):
    from app.activities import openclaw_activities as oa
    from app.llm.model_policy import TASK_THINKING, THINKING_DEFAULT

    seen = []

    async def dispatch(session_key, message, **kwargs):
        seen.append(kwargs.get("thinking"))
        return "done"

    monkeypatch.setattr(oa, "_dispatch_openclaw_session", dispatch)
    await oa.send_to_openclaw({"task_id": "u1", "message": "summarize my notes"})
    await oa.send_to_openclaw(
        {"task_id": "c1", "message": "RMP CANARY", "tags": ["canary", "system"], "task_type": "canary"}
    )
    assert seen == [TASK_THINKING, THINKING_DEFAULT]


def test_rework_dispatch_carries_task_type_and_tags():
    import inspect

    from app.workflows import generic_task

    src = inspect.getsource(generic_task.GenericTaskWorkflow._judge_and_deliver)
    rework = src[src.find('"message": rework_prompt'):]
    rework = rework[: rework.find("}")]
    assert '"task_type": task_type' in rework
    assert '"tags": tags' in rework


def test_no_deadline_outside_an_activity():
    from app.activities import openclaw_activities as oa

    assert oa._activity_deadline(5.0) is None
