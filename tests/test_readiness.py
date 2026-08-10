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
