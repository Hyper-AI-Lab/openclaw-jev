import asyncio

import pytest

from app.temporal_control import connect_temporal_with_retry


@pytest.mark.asyncio
async def test_connect_temporal_with_retry_succeeds_after_failures(monkeypatch):
    calls = {"n": 0}

    async def fake_connect(addr, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("not ready")
        return "client"

    async def _no_sleep(_d):
        return None

    monkeypatch.setattr("app.temporal_control.Client.connect", fake_connect)
    monkeypatch.setattr("app.temporal_control.get_temporal_client_kwargs", lambda: {})
    monkeypatch.setattr("app.temporal_control.asyncio.sleep", _no_sleep)
    client = await connect_temporal_with_retry(attempts=5, delay_sec=0.01)
    assert client == "client"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_connect_temporal_with_retry_raises_after_exhaustion(monkeypatch):
    async def fake_connect(addr, **kwargs):
        raise ConnectionError("down")

    async def _no_sleep(_d):
        return None

    monkeypatch.setattr("app.temporal_control.Client.connect", fake_connect)
    monkeypatch.setattr("app.temporal_control.get_temporal_client_kwargs", lambda: {})
    monkeypatch.setattr("app.temporal_control.asyncio.sleep", _no_sleep)
    with pytest.raises(ConnectionError):
        await connect_temporal_with_retry(attempts=3, delay_sec=0.01)


@pytest.mark.asyncio
async def test_reconciler_reconnects_after_dead_client(monkeypatch):
    from app import reconciler

    class Dead:
        class service_client:
            @staticmethod
            async def check_health():
                raise ConnectionError("dead")

    class Live:
        class service_client:
            @staticmethod
            async def check_health():
                return True

    monkeypatch.setattr(reconciler, "_temporal_client", Dead())

    async def fake_retry(**kwargs):
        return Live()

    monkeypatch.setattr(
        "app.temporal_control.connect_temporal_with_retry", fake_retry
    )
    client = await reconciler._get_temporal()
    assert client is reconciler._temporal_client
    assert client is not None
