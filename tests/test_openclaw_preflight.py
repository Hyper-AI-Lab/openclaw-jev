"""The OpenClaw upgrade pre-flight: Node ranges, transcript format, patches, and what it reports."""
import json
import shutil

import pytest

from ops import openclaw_preflight as pf

# engines.node of 2026.9.1 and 9.2, and of 2026.9.3 and later.
NODE_22_OK = ">=22.22.3 <23 || >=24.15.0 <25 || >=25.9.0"
NODE_24_ONLY = ">=24.16.0 <25 || >=26.1.0"
TABLE_91 = ("CREATE TABLE IF NOT EXISTS transcript_events (\n  session_id TEXT NOT NULL,\n"
            "  seq INTEGER NOT NULL,\n  event_json TEXT NOT NULL,\n  created_at INTEGER NOT NULL")
TABLE_97 = ("CREATE TABLE IF NOT EXISTS transcript_events (\\n  session_id TEXT NOT NULL,\\n"
            "  seq INTEGER NOT NULL,\\n  event_json TEXT,\\n  created_at INTEGER NOT NULL,\\n  event_zstd BLOB,\\n"
            "  event_utf8_bytes INTEGER CHECK (event_utf8_bytes IS NULL OR event_utf8_bytes >= 0),\\n"
            "  navigation_json TEXT")
TABLE_UNKNOWN = ("CREATE TABLE IF NOT EXISTS transcript_events (\\n  session_id TEXT NOT NULL,\\n"
                 "  seq INTEGER NOT NULL,\\n  payload BLOB NOT NULL,\\n  created_at INTEGER NOT NULL")
needs_node = pytest.mark.skipif(not (shutil.which("node") and shutil.which("npm")), reason="needs node and npm")


@pytest.mark.parametrize("version, spec, ok", [
    ("v22.23.2", NODE_22_OK, True),
    ("v22.22.2", NODE_22_OK, False),
    ("v22.23.2", NODE_24_ONLY, False),
    ("v24.16.0", NODE_24_ONLY, True),
    ("v25.0.0", NODE_24_ONLY, False),
    ("v26.1.0", NODE_24_ONLY, True),
])
def test_node_ranges_as_openclaw_writes_them(version, spec, ok):
    assert pf.satisfies(version, spec) is ok


@pytest.mark.parametrize("spec", ["^24.16.0", "~24.16.0", ">24", "<=24.16", "24.x"])
def test_a_range_it_cannot_read_is_an_error_not_a_pass(spec):
    with pytest.raises(ValueError):
        pf.satisfies("v24.16.0", spec)


def test_every_transcript_table_definition_is_read_for_its_layout(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "migrate.js").write_text(
        'const start = SCHEMA.indexOf("CREATE TABLE IF NOT EXISTS transcript_events (", from);')
    assert pf.transcript_layouts(dist) == []
    (dist / "worker.mjs").write_text("const ddl = `" + TABLE_91 + "`;")
    (dist / "maintenance.js").write_text('const ddl = "' + TABLE_91.replace("\n", "\\n") + '";')
    assert sorted(pf.transcript_layouts(dist)) == ["plain", "plain"]
    (dist / "store.mjs").write_text('const ddl = "' + TABLE_97 + '";')
    assert sorted(pf.transcript_layouts(dist)) == ["plain", "plain", "zstd"]
    (dist / "later.mjs").write_text('const ddl = "' + TABLE_UNKNOWN + '";')
    assert "unknown" in pf.transcript_layouts(dist)


def test_the_patcher_rehearses_on_the_dist_it_is_given(tmp_path):
    (tmp_path / "dist").mkdir()
    applied, detail = pf.patches_apply(tmp_path / "dist")
    assert not applied
    assert detail == "ERROR: required patch missing after apply: hook-persistence"


def package(tmp_path, node_range, table):
    pkg = tmp_path / "pkg"
    (pkg / "dist").mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps(
        {"name": "openclaw", "version": "2026.9.7", "engines": {"node": node_range}}))
    (pkg / "dist" / "store.mjs").write_text('const ddl = "' + table + '";')
    return str(pkg)


@needs_node
def test_a_target_that_fails_is_refused_with_every_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pf, "patches_apply", lambda dist: (False, "ERROR: required patch missing after apply: x"))
    assert pf.main(package(tmp_path, ">=99.0.0", TABLE_UNKNOWN)) == 1
    out = capsys.readouterr().out
    assert "target: openclaw@2026.9.7" in out
    assert "FAIL: it needs Node >=99.0.0; this host runs v" in out
    assert "FAIL: RMP's patches do not apply: ERROR: required patch missing after apply: x" in out
    assert "FAIL: it stores transcript events in a layout RMP's reader does not know (found: unknown)" in out


@needs_node
@pytest.mark.parametrize("table", [TABLE_91.replace("\n", "\\n"), TABLE_97])
def test_a_target_that_passes_is_cleared(tmp_path, monkeypatch, capsys, table):
    monkeypatch.setattr(pf, "patches_apply", lambda dist: (True, ""))
    assert pf.main(package(tmp_path, ">=18.0.0", table)) == 0
    assert "OK:" in capsys.readouterr().out
