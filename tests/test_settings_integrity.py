"""settings.json: reads never write, writes are locked and atomic, nothing is lost."""
import json
import multiprocessing
import os

import pytest

import app.config as config

JEV = {"intake_mode": "enforce", "promotion_mode": "shadow"}


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"api_key": "k1", "jev": JEV, "task_registry": {"x": 1}}, indent=2))
    monkeypatch.setattr(config, "SETTINGS_PATH", str(path))
    monkeypatch.delenv("RMP_API_KEY", raising=False)
    return path


def test_reading_settings_never_rewrites_the_file(settings_file):
    before = settings_file.read_bytes()
    stamp = settings_file.stat().st_mtime_ns
    for _ in range(20):
        merged = config.load_settings()
    assert merged["jev"] == JEV
    assert merged["task_registry"]["x"] == 1
    assert "intake_model" in merged["task_registry"]
    assert settings_file.read_bytes() == before
    assert settings_file.stat().st_mtime_ns == stamp


def test_update_keeps_unknown_keys_and_does_not_store_defaults(settings_file):
    merged = config.update_settings(lambda s: s.update(development_mode=True))
    stored = json.loads(settings_file.read_text())
    assert stored["jev"] == JEV and stored["development_mode"] is True
    assert set(stored["task_registry"]) == {"x"}
    assert merged["development_mode"] is True and "vector_memory" in merged


def test_corrupt_settings_raise_instead_of_reading_as_empty(settings_file):
    settings_file.write_text('{"api_key": "k1", "jev": ')
    with pytest.raises(config.SettingsCorruptError):
        config.load_settings()
    assert settings_file.read_text() == '{"api_key": "k1", "jev": '


def test_env_api_key_is_used_but_not_written(settings_file, monkeypatch):
    monkeypatch.setenv("RMP_API_KEY", "from-env")
    assert config.load_settings()["api_key"] == "from-env"
    assert json.loads(settings_file.read_text())["api_key"] == "k1"


def _first_read(queue):
    queue.put(config.load_settings()["api_key"])


def test_missing_api_key_is_created_once_across_processes(settings_file):
    settings_file.write_text(json.dumps({"jev": JEV}))
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_first_read, args=(queue,)) for _ in range(6)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    keys = {queue.get(timeout=5) for _ in procs}
    stored = json.loads(settings_file.read_text())
    assert keys == {stored["api_key"]} and stored["jev"] == JEV


def _writer(index, rounds):
    for _ in range(rounds):
        config.update_settings(lambda s: s.update({f"n{index}": s.get(f"n{index}", 0) + 1}))


def _reader(rounds, errors):
    for _ in range(rounds):
        try:
            merged = config.load_settings()
            if merged.get("jev") != JEV or merged.get("api_key") != "k1":
                errors.put("lost keys")
        except Exception as exc:
            errors.put(repr(exc))


def test_concurrent_writers_and_readers_lose_nothing(settings_file):
    ctx = multiprocessing.get_context("fork")
    errors = ctx.Queue()
    writers = [ctx.Process(target=_writer, args=(i, 40)) for i in range(4)]
    readers = [ctx.Process(target=_reader, args=(300, errors)) for _ in range(4)]
    for p in writers + readers:
        p.start()
    for p in writers + readers:
        p.join(120)
    assert all(p.exitcode == 0 for p in writers + readers)
    assert errors.empty()
    stored = json.loads(settings_file.read_text())
    assert stored["jev"] == JEV and stored["api_key"] == "k1"
    assert [stored[f"n{i}"] for i in range(4)] == [40] * 4
    leftovers = [n for n in os.listdir(settings_file.parent) if n.endswith(".tmp")]
    assert leftovers == []
