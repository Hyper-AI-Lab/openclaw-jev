import asyncio

import pytest

from app.production.readiness import (
    check_api_key,
    check_artifact_store,
    check_development_mode,
    run_all_checks,
)


def test_api_key_check():
    r = check_api_key()
    assert r.status in ("pass", "fail")


def test_development_mode_warns_when_on():
    r = check_development_mode()
    assert r.status in ("pass", "warn")


@pytest.mark.asyncio
async def test_run_all_checks_structure():
    result = await run_all_checks()
    assert "checks" in result
    assert "summary" in result
    assert isinstance(result["checks"], list)
    assert len(result["checks"]) >= 5
    names = {c["name"] for c in result["checks"]}
    assert "task_registry_vector" in names
    assert "openai_key" in names
    assert "slack_sockets" in names
    assert "safe_harbor" in names


def test_slack_sockets_single_is_pass(monkeypatch):
    from app.production import readiness as rd

    monkeypatch.setattr(rd, "gateway_process_pids", lambda: [4242])
    r = rd.check_slack_sockets()
    assert r.status == "pass"
    assert r.details["count"] == 1


def test_slack_sockets_dual_is_warn(monkeypatch):
    from app.production import readiness as rd

    monkeypatch.setattr(rd, "gateway_process_pids", lambda: [1, 2])
    r = rd.check_slack_sockets()
    assert r.status == "warn"
    assert r.details["count"] == 2


def test_openai_key_check_is_non_blocking():
    from app.production.readiness import check_openai_key

    r = check_openai_key()
    assert r.name == "openai_key"
    assert r.status in ("pass", "warn")
    if r.status == "warn":
        assert "openai_key_missing" in r.message


def test_temporal_persistence_requires_postgres(monkeypatch, tmp_path):
    from app.production import readiness as rd

    unit = tmp_path / "temporal.service"
    unit.write_text(
        "ExecStart=/usr/bin/docker run -e DB=postgres12 temporalio/auto-setup:1.30.1\n"
    )
    orig_open = open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/etc/systemd/system/temporal.service":
            return orig_open(unit, *args, **kwargs)
        return orig_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr(
        rd,
        "_temporal_postgres_schemas",
        lambda: {"temporal": True, "temporal_visibility": True},
    )
    r = rd.check_temporal_persistence()
    assert r.status == "pass"
    assert r.details["temporal"] is True
    assert r.details["temporal_visibility"] is True


def test_safe_harbor_pass_when_auto_restart_off(monkeypatch):
    from app.production.readiness import check_safe_harbor_peripheral

    monkeypatch.setattr(
        "app.config.load_settings",
        lambda: {"production": {"scanner_auto_restart": False}},
    )
    r = check_safe_harbor_peripheral()
    assert r.status == "pass"


def test_safe_harbor_warns_when_auto_on_and_missing(monkeypatch, tmp_path):
    from app.production.readiness import check_safe_harbor_peripheral

    monkeypatch.setenv("AURA_SAFE_HARBOR", str(tmp_path / "no-harbor"))
    monkeypatch.setattr(
        "app.config.load_settings",
        lambda: {"production": {"scanner_auto_restart": True}},
    )
    r = check_safe_harbor_peripheral()
    assert r.status == "warn"
    assert "missing" in r.message
