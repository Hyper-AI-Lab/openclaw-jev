"""Intake path metrics."""
import os
import tempfile

import pytest

from app import metrics


@pytest.fixture
def isolated_metrics(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "metrics_counters.json")
        monkeypatch.setenv("RMP_METRICS_PATH", path)
        metrics._save(metrics._empty(), [])
        yield path


def test_record_intake_run_workflow_path(isolated_metrics):
    metrics.record_intake_run(path="workflow", latency_ms=1200, confidence=90)
    counters = metrics.get_counters()
    assert counters["intake_workflow_ok"] == 1
    assert counters["intake_degraded"] == 0
    assert metrics.get_intake_latency_samples()[-1] == 1200


def test_record_intake_run_degraded_path(isolated_metrics):
    metrics.record_intake_run(path="deterministic", latency_ms=45000, confidence=0)
    counters = metrics.get_counters()
    assert counters["intake_degraded"] == 1
    assert counters["intake_workflow_ok"] == 0
