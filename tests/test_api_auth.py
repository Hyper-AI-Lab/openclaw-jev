"""A request without the RMP API key is refused with 401, not a server error."""
from fastapi.testclient import TestClient


def test_missing_or_wrong_key_gets_401(monkeypatch):
    from app.api import server

    monkeypatch.setattr(server, "get_api_key", lambda: "k" * 64)
    client = TestClient(server.app)
    assert client.get("/tasks/by-idempotency/x").status_code == 401
    assert client.get("/tasks/by-idempotency/x", headers={"X-RMP-API-Key": "wrong"}).json() == {
        "detail": "Invalid or missing RMP API key"}
    assert client.get("/health").status_code != 401
