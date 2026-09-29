import asyncio
import json

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


def _stored_settings(tmp_path, monkeypatch, content):
    import app.config as config

    path = tmp_path / "settings.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    monkeypatch.setattr(config, "SETTINGS_PATH", str(path))


def test_settings_integrity_passes_with_a_valid_jev_section(tmp_path, monkeypatch):
    from app.production.readiness import check_settings_integrity

    _stored_settings(tmp_path, monkeypatch, {"api_key": "k" * 64, "jev": {"intake_mode": "enforce", "promotion_mode": "shadow"}})
    monkeypatch.setenv("RMP_API_KEY", "k" * 64)
    r = check_settings_integrity()
    assert r.status == "pass" and r.details["jev"] == {"intake_mode": "enforce", "promotion_mode": "shadow"}


def test_settings_integrity_fails_when_jev_is_missing_invalid_or_keys_differ(tmp_path, monkeypatch):
    from app.production.readiness import check_settings_integrity

    monkeypatch.delenv("RMP_API_KEY", raising=False)
    _stored_settings(tmp_path, monkeypatch, {"api_key": "k" * 64})
    assert "jev section missing" in check_settings_integrity().message
    _stored_settings(tmp_path, monkeypatch, {"api_key": "k" * 64, "jev": {"intake_mode": "always"}})
    assert "jev section invalid" in check_settings_integrity().message
    _stored_settings(tmp_path, monkeypatch, '{"api_key": ')
    assert check_settings_integrity().status == "fail"
    _stored_settings(tmp_path, monkeypatch, {"api_key": "a" * 64, "jev": {}})
    monkeypatch.setenv("RMP_API_KEY", "b" * 64)
    r = check_settings_integrity()
    assert r.status == "fail" and "disagree" in r.message


def test_registry_freshness_counts_finished_user_tasks_only(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db.models import Base, Task, TaskRegistryEntry
    from app.production.readiness import check_task_registry_index_fresh

    url = f"sqlite:///{tmp_path / 'registry.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine, tables=[Task.__table__, TaskRegistryEntry.__table__])
    canary = "RMP CANARY: Reply with exactly CANARY_OK on its own line. No tools."
    with Session(engine) as db:
        db.add_all([Task(id=f"c{i}", goal=canary, task_type="canary", status="completed") for i in range(50)])
        db.add_all([Task(id=f"u{i}", goal="Compare visa rules", status="completed") for i in range(10)])
        db.add_all([TaskRegistryEntry(id=f"r{i}", task_id=f"u{i}") for i in range(9)])
        db.commit()
    monkeypatch.setattr("app.db.database.DATABASE_URL", url)
    r = check_task_registry_index_fresh()
    assert r.status == "pass" and r.details == {"indexed": 9, "terminal": 10}

    with Session(engine) as db:
        db.query(TaskRegistryEntry).filter(TaskRegistryEntry.task_id.in_(["u5", "u6", "u7", "u8"])).delete()
        db.commit()
    r = check_task_registry_index_fresh()
    assert r.status == "warn" and "5/10 finished user tasks" in r.message
