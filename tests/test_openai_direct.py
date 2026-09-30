"""Direct memory-model client: streaming, timeouts, fallback, breaker, budget, lane, ledger."""
import asyncio
import json
from typing import List

import httpx
import pytest
from pydantic import BaseModel

from app.llm import openai_direct as od
from app.llm import quota_broker as qb
from app.llm import usage_monitor as um


class Summary(BaseModel):
    summary: str
    points: List[str]


GOOD = {"summary": "Kirill's code word is PELICAN-47.", "points": ["code word"]}
FAST = od.LanePolicy(
    call_deadline_sec=5.0,
    openai_first_output_sec=0.3,
    openai_gap_sec=0.2,
    nvidia_first_event_sec=0.3,
    nvidia_gap_sec=0.2,
    breaker_cooldown_sec=30.0,
)


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for delay, chunk in self.chunks:
            if delay:
                await asyncio.sleep(delay)
            yield chunk

    async def aclose(self):
        pass


def _event(name, payload):
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()


def openai_stream(text, *, delay_before_output=0.0, gap=0.0, usage=None):
    usage = usage or {"input_tokens": 120, "input_tokens_details": {"cached_tokens": 20}, "output_tokens": 40}
    chunks = [
        (0.0, _event("response.created", {"type": "response.created", "response": {"id": "r1"}})),
        (0.0, _event("response.in_progress", {"type": "response.in_progress"})),
        (delay_before_output, _event("response.output_item.added", {"type": "response.output_item.added"})),
    ]
    for piece in (text[: len(text) // 2], text[len(text) // 2 :]):
        chunks.append((gap, _event("response.output_text.delta", {"type": "response.output_text.delta", "delta": piece})))
    chunks.append(
        (
            0.0,
            _event(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                        "usage": usage,
                    },
                },
            ),
        )
    )
    return _Stream(chunks)


def nvidia_stream(text, *, first_delay=0.0):
    chunks = [(first_delay, f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\n".encode())]
    chunks.append((0.0, f"data: {json.dumps({'choices': [], 'usage': {'prompt_tokens': 90, 'completion_tokens': 30}})}\n\n".encode()))
    chunks.append((0.0, b"data: [DONE]\n\n"))
    return _Stream(chunks)


@pytest.fixture
def env(monkeypatch, tmp_path):
    env_file = tmp_path / "openclaw.env"
    env_file.write_text("NVIDIA_API_KEY=nv-test\n")
    monkeypatch.setattr(qb, "STATE_PATH", tmp_path / "llm_quota.json")
    monkeypatch.setattr(qb, "LOCK_PATH", tmp_path / ".llm_quota.lock")
    monkeypatch.setattr(qb, "OPENCLAW_ENV_PATH", env_file)
    monkeypatch.setattr(qb, "_patch_auth_last_good", lambda pid: None)
    monkeypatch.setattr(qb, "_patch_auth_profile_cooldown", lambda pid, until: None)
    monkeypatch.setattr(qb, "_today_usage_by_profile", lambda: {})
    monkeypatch.setattr(um, "USAGE_PATH", tmp_path / "llm_usage.json")
    monkeypatch.setattr(um, "LOCK_PATH", tmp_path / ".llm_usage.lock")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(od, "lane_policy", lambda: FAST)
    return tmp_path


def install(handler):
    transport = od._Transport(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    od._transports[asyncio.get_running_loop()] = transport
    return transport


def ledger_entries():
    store = um._read_store()
    return [e for e in store.get("rolling_24h", []) if e["source"] == "memory_llm"]


async def _call(**kw):
    return await od.structured_call(
        Summary, purpose="test", instructions="Summarize.", input_text="Kirill: my code word is PELICAN-47", **kw
    )


@pytest.mark.asyncio
async def test_openai_structured_success_records_ledger_and_frees_lane(env):
    seen = []

    async def handler(request):
        seen.append(json.loads(request.content))
        assert request.url.path == "/v1/responses"
        return httpx.Response(200, stream=openai_stream(json.dumps(GOOD)))

    install(handler)
    result = await _call(priority="recall")
    assert result.value.summary.startswith("Kirill")
    assert result.model == "gpt-6-luna" and result.attempts == 1
    assert (result.input_tokens, result.cached_tokens, result.output_tokens) == (120, 20, 40)
    body = seen[0]
    assert body["store"] is False and body["stream"] is True
    assert body["reasoning"] == {"effort": "medium"}
    fmt = body["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert fmt["schema"]["additionalProperties"] is False
    assert body["instructions"].startswith(od.DATA_RULE)
    entries = ledger_entries()
    assert len(entries) == 1 and entries[0]["input_tokens"] == 120 and entries[0]["profile_id"] == "openai:default"
    lane = qb.get_orchestration_status()["memory_lane"]
    assert lane["active"] == 0 and lane["recall_waiting"] == 0


@pytest.mark.asyncio
async def test_reasoning_silence_before_output_is_within_first_output_budget(env):
    async def handler(request):
        # 0.25 s of silence after the lifecycle events: longer than the 0.2 s gap, inside 0.3 s.
        return httpx.Response(200, stream=openai_stream(json.dumps(GOOD), delay_before_output=0.25))

    install(handler)
    result = await _call()
    assert result.model == "gpt-6-luna" and result.attempts == 1


@pytest.mark.asyncio
async def test_no_first_output_falls_back_to_nvidia(env):
    calls = []

    async def handler(request):
        calls.append(request.url.host)
        if request.url.host == "api.openai.com":
            return httpx.Response(200, stream=openai_stream(json.dumps(GOOD), delay_before_output=1.0))
        body = json.loads(request.content)
        assert body["model"] == "openai/gpt-oss-20b"
        assert body["response_format"]["type"] == "json_schema"
        return httpx.Response(200, stream=nvidia_stream(json.dumps(GOOD)))

    install(handler)
    result = await _call()
    assert calls == ["api.openai.com", "api.openai.com", "integrate.api.nvidia.com"]
    assert result.model == "openai/gpt-oss-20b" and result.attempts == 3
    assert (result.input_tokens, result.output_tokens) == (90, 30)
    assert [e["profile_id"] for e in ledger_entries()] == ["nvidia:default"]


@pytest.mark.asyncio
async def test_gap_after_output_started_is_an_idle_timeout(env):
    async def handler(request):
        if request.url.host == "api.openai.com":
            return httpx.Response(200, stream=openai_stream(json.dumps(GOOD), gap=0.5))
        return httpx.Response(200, stream=nvidia_stream(json.dumps(GOOD)))

    install(handler)
    result = await _call()
    assert result.model == "openai/gpt-oss-20b"


@pytest.mark.asyncio
async def test_invalid_output_retries_then_falls_back_without_opening_the_circuit(env):
    async def handler(request):
        if request.url.host == "api.openai.com":
            return httpx.Response(200, stream=openai_stream('{"summary": "missing points"}'))
        return httpx.Response(200, stream=nvidia_stream(json.dumps(GOOD)))

    transport = install(handler)
    result = await _call()
    assert result.model == "openai/gpt-oss-20b" and result.attempts == 3
    assert not transport.breakers["openai"].is_open()
    # Both invalid OpenAI answers were billed.
    assert [e["profile_id"] for e in ledger_entries()].count("openai:default") == 2


@pytest.mark.asyncio
async def test_non_retryable_openai_error_skips_second_attempt(env):
    calls = []

    async def handler(request):
        calls.append(request.url.host)
        if request.url.host == "api.openai.com":
            return httpx.Response(400, json={"error": {"message": "bad schema"}})
        return httpx.Response(200, stream=nvidia_stream(json.dumps(GOOD)))

    install(handler)
    result = await _call()
    assert calls == ["api.openai.com", "integrate.api.nvidia.com"]
    assert result.model == "openai/gpt-oss-20b"


@pytest.mark.asyncio
async def test_breaker_opens_after_repeated_outages_and_fails_fast(env):
    calls = []

    async def handler(request):
        calls.append(request.url.host)
        return httpx.Response(503, text="overloaded")

    transport = install(handler)
    with pytest.raises(od.DirectModelError) as first:
        await _call()
    assert first.value.reason == "http_503"
    # Each provider has its own circuit; three outages open each one.
    for _ in range(2):
        with pytest.raises(od.DirectModelError):
            await _call()
    assert transport.breakers["openai"].is_open() and transport.breakers["nvidia"].is_open()
    before = len(calls)
    with pytest.raises(od.DirectModelError) as err:
        await _call()
    assert len(calls) == before, "open circuits send nothing"
    assert err.value.reason == "circuit_open"


@pytest.mark.asyncio
async def test_openai_429_opens_circuit_for_retry_after_without_touching_openclaw_auth(env, monkeypatch):
    touched = []
    monkeypatch.setattr(qb, "record_rate_limit", lambda *a, **k: touched.append(a))

    async def handler(request):
        if request.url.host == "api.openai.com":
            return httpx.Response(429, headers={"retry-after": "90"}, text="slow down")
        return httpx.Response(200, stream=nvidia_stream(json.dumps(GOOD)))

    transport = install(handler)
    result = await _call()
    assert result.model == "openai/gpt-oss-20b"
    assert transport.breakers["openai"].blocked_until - __import__("time").monotonic() > 60
    assert touched == [], "OpenAI 429s must not cool Aura's openai:default profile"


@pytest.mark.asyncio
async def test_nvidia_410_is_reported_as_gone(env):
    async def handler(request):
        if request.url.host == "api.openai.com":
            return httpx.Response(500, text="boom")
        return httpx.Response(410, text="gone")

    install(handler)
    with pytest.raises(od.DirectModelError) as err:
        await _call()
    assert err.value.reason == "model_gone"


@pytest.mark.asyncio
async def test_daily_budget_stops_calls_before_any_request(env, monkeypatch):
    monkeypatch.setattr(od, "lane_policy", lambda: od.LanePolicy(daily_token_budget=100))
    um.record_request("openai:default", "memory_llm", input_tokens=80, output_tokens=30, model="gpt-6-luna")
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=openai_stream(json.dumps(GOOD)))

    install(handler)
    with pytest.raises(od.DirectBudgetExceeded):
        await _call()
    assert calls == []


def test_lane_admits_recall_first_and_limits_enrichment_while_users_run(env):
    kw = dict(concurrency=2, per_minute=60, busy_enrich_slots=1)
    held = qb.reserve_memory_lane_slot("enrich", concurrency=1, per_minute=60, busy_enrich_slots=1)
    assert held
    # The lane is full for this recall, so it waits, and its waiting holds enrichment back.
    assert qb.reserve_memory_lane_slot("recall", concurrency=1, per_minute=60, busy_enrich_slots=1, waiter_id="w1") is None
    assert qb.reserve_memory_lane_slot("enrich", **kw) is None
    recall = qb.reserve_memory_lane_slot("recall", waiter_id="w1", **kw)
    assert recall
    assert qb.get_orchestration_status()["memory_lane"]["recall_waiting"] == 0
    qb.release_memory_lane_slot(held)
    # Recall admitted: enrichment may take the free slot while no user run holds a broker slot.
    enrich = qb.reserve_memory_lane_slot("enrich", **kw)
    assert enrich
    assert qb.reserve_memory_lane_slot("enrich", **kw) is None, "lane full"
    qb.release_memory_lane_slot(enrich)
    qb.release_memory_lane_slot(recall)

    def busy(state):
        state.setdefault("global", {}).setdefault("active_slots", {})["s1"] = {
            "kind": "user", "session_key": "agent:main:rmp_task_x",
        }

    qb._mutate_state(busy)
    first = qb.reserve_memory_lane_slot("enrich", concurrency=3, per_minute=60, busy_enrich_slots=1)
    assert first
    assert qb.reserve_memory_lane_slot("enrich", concurrency=3, per_minute=60, busy_enrich_slots=1) is None
    assert qb.reserve_memory_lane_slot("recall", concurrency=3, per_minute=60, busy_enrich_slots=1)


def test_lane_per_minute_cap_and_stale_slot_reaping(env, monkeypatch):
    assert qb.reserve_memory_lane_slot("enrich", concurrency=5, per_minute=1, busy_enrich_slots=1)
    assert qb.reserve_memory_lane_slot("enrich", concurrency=5, per_minute=1, busy_enrich_slots=1) is None
    real_now = qb._now_ms
    monkeypatch.setattr(qb, "_now_ms", lambda: real_now() + qb.MEMORY_LANE_STALE_MS + 61_000)
    assert qb.reserve_memory_lane_slot("enrich", concurrency=1, per_minute=1, busy_enrich_slots=1)
    status = qb.get_orchestration_status()["memory_lane"]
    assert status["active"] == 1


@pytest.mark.asyncio
async def test_lane_timeout_when_no_slot_frees(env, monkeypatch):
    monkeypatch.setattr(od, "lane_policy", lambda: od.LanePolicy(concurrency=1, call_deadline_sec=1.0))
    held = qb.reserve_memory_lane_slot("recall", concurrency=1, per_minute=60, busy_enrich_slots=1)
    assert held

    async def handler(request):
        raise AssertionError("no request without a slot")

    install(handler)
    with pytest.raises(od.DirectModelError) as err:
        await _call()
    assert err.value.reason == "lane_timeout"


def test_direct_tokens_count_toward_the_24h_budget_but_not_the_abort_rate(env):
    um.record_request("openai:default", "memory_llm", input_tokens=4_000, output_tokens=500, model="gpt-6-luna")
    now_ms = int(__import__("time").time() * 1000)
    direct = um._direct_usage(now_ms - 3_600_000, now_ms)
    assert direct == {"memory_llm": {"requests": 1, "input_tokens": 4_000, "output_tokens": 500}}
    report = {"available": True, "totals": um._zero_attribution(), "abort_rate": 0.0, "direct": direct}
    assert um.usage_alerts(report, input_budget=3_000) == ["24 h prompt tokens 4,000 over budget 3,000"]
    assert um.usage_alerts(report, input_budget=5_000) == []
    assert um.source_tokens_today("memory_llm") == 4_500
