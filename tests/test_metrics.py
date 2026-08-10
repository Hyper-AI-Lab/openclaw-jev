import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import metrics


@pytest.fixture
def isolated_metrics(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "metrics_counters.json")
        monkeypatch.setenv("RMP_METRICS_PATH", path)
        metrics._save(metrics._empty(), [])
        yield path


def test_inc_and_format(isolated_metrics):
    metrics.inc("task_created")
    metrics.inc("task_created")
    metrics.inc("task_failed")
    text = metrics.format_prometheus()
    assert "rmp_task_created_total 2" in text
    assert "rmp_task_failed_total 1" in text
    assert "rmp_stale_detected_total 0" in text


def test_metrics_endpoint(isolated_metrics):
    from app.api.server import app

    metrics.inc("cron_reconcile")
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")
    assert "rmp_cron_reconcile_total 1" in resp.text
