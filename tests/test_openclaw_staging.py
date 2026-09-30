"""The staging gateway stays away from production: config, stored paths, plugin, units."""
import json
import sqlite3
from pathlib import Path

import pytest

from ops import openclaw_staging as staging


@pytest.fixture
def trees(tmp_path, monkeypatch):
    prod, stage = tmp_path / "prod", tmp_path / "stage"
    oc = stage / "home" / ".openclaw"
    for path in (prod / "workspace", oc / "state", oc / "plugins" / "rmp_adapter"):
        path.mkdir(parents=True)
    monkeypatch.setattr(staging, "PROD", prod)
    monkeypatch.setattr(staging, "STAGE", stage)
    monkeypatch.setattr(staging, "HOME", stage / "home")
    monkeypatch.setattr(staging, "OC", oc)
    monkeypatch.setattr(staging, "PREFIX", stage / "npm-global")
    return prod, oc


def test_the_config_has_its_own_port_and_tokens_and_no_slack_cron_or_production_paths(trees):
    prod, oc = trees
    (prod / "openclaw.json").write_text(json.dumps({
        "gateway": {"port": 18789, "auth": {"token": "prod-gateway"}},
        "hooks": {"token": "prod-hooks", "allowedSessionKeyPrefixes": ["agent:main:rmp_"]},
        "channels": {"slack": {"enabled": True, "botToken": "xoxb-prod", "appToken": "xapp-prod"}},
        "plugins": {"entries": {"slack": {"enabled": True}}, "load": {"paths": [f"{prod}/plugins/rmp_adapter"]}},
        "agents": {"defaults": {"workspace": f"{prod}/workspace"}},
    }))
    tokens = staging.write_config()
    config = json.loads((oc / "openclaw.json").read_text())
    assert config["gateway"]["port"] == staging.PORT
    assert config["gateway"]["auth"]["token"] == tokens["gateway"] != "prod-gateway"
    assert config["hooks"]["token"] == tokens["hooks"] != "prod-hooks"
    assert config["channels"]["slack"] == {"enabled": False}
    assert config["plugins"]["entries"]["slack"]["enabled"] is False
    assert config["cron"]["enabled"] is False
    assert config["plugins"]["load"]["paths"] == [f"{oc}/plugins/rmp_adapter"]
    assert str(prod) not in (oc / "openclaw.json").read_text()


def test_stored_production_paths_are_rewritten_and_the_copied_lease_dropped(trees):
    prod, oc = trees
    con = sqlite3.connect(oc / "state" / "openclaw.sqlite")
    con.executescript(f"""
        CREATE TABLE agent_database_leases (lease_id TEXT, path TEXT, owner_pid TEXT);
        INSERT INTO agent_database_leases VALUES ('l1', '{prod}/agents/main/agent/openclaw-agent.sqlite', '1356539');
        CREATE TABLE exec_approvals_config (socket_path TEXT, raw_json TEXT);
        INSERT INTO exec_approvals_config VALUES ('{prod}/exec-approvals.sock', '{{"socket": "{prod}/exec-approvals.sock"}}');
        CREATE TABLE cron_jobs (store_key TEXT, n INTEGER);
        INSERT INTO cron_jobs VALUES ('{prod}/cron/jobs.json', 1);
        CREATE TABLE audit_events (payload TEXT);
        INSERT INTO audit_events VALUES ('{prod}/openclaw.json');
    """)
    con.commit()
    con.close()
    assert staging.rewrite_state_paths() == 4  # lease path, socket, raw config, cron store
    con = sqlite3.connect(oc / "state" / "openclaw.sqlite")
    assert con.execute("SELECT COUNT(*) FROM agent_database_leases").fetchone()[0] == 0
    assert con.execute("SELECT socket_path, raw_json FROM exec_approvals_config").fetchone() == (
        f"{oc}/exec-approvals.sock", f'{{"socket": "{oc}/exec-approvals.sock"}}')
    assert con.execute("SELECT store_key, n FROM cron_jobs").fetchone() == (f"{oc}/cron/jobs.json", 1)
    # History with integrity chains is left as it was.
    assert con.execute("SELECT payload FROM audit_events").fetchone()[0] == f"{prod}/openclaw.json"


def test_the_rmp_plugin_copy_calls_a_closed_port(trees):
    _, oc = trees
    index = oc / "plugins" / "rmp_adapter" / "index.js"
    index.write_text("const LOG = '/root/.openclaw/logs/rmp_adapter.log';\nconst RMP_API = 'http://127.0.0.1:8000';\n")
    staging.isolate_rmp_plugin()
    text = index.read_text()
    assert f"const RMP_API = '{staging.DEAD_RMP}';" in text and "127.0.0.1:8000" not in text
    assert f"const LOG = '{oc}/logs/rmp_adapter.log';" in text
    index.write_text("const RMP_API = process.env.RMP_API;\n")
    with pytest.raises(SystemExit, match="could not isolate"):
        staging.isolate_rmp_plugin()


def test_staged_commands_run_the_staging_binaries_in_a_hardened_unit(trees, tmp_path, monkeypatch):
    _, oc = trees
    node_bin = tmp_path / "node24" / "bin"
    node_bin.mkdir(parents=True)
    (staging.PREFIX / "bin").mkdir(parents=True)
    for exe in (staging.PREFIX / "bin" / "openclaw", node_bin / "node"):
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    monkeypatch.setattr(staging, "node_bin", lambda: node_bin)
    argv = staging.unit_argv("openclaw-staging", ["openclaw", "gateway", "--port", "19789"], wait=False)
    command = argv[argv.index("--") + 1:]
    assert command == [str(staging.PREFIX / "bin" / "openclaw"), "gateway", "--port", "19789"]
    props = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    for required in ("ProtectSystem=strict", "ProtectHome=read-only", f"ReadWritePaths={staging.STAGE}",
                     "NoNewPrivileges=yes", "PrivateTmp=yes"):
        assert required in props
    hidden = next(p for p in props if p.startswith("InaccessiblePaths=")).split("=", 1)[1].split()
    assert {"-/run/systemd/private", "-/run/dbus", "-/etc/rmp", "-/etc/aura-coder"} <= set(hidden)
    env = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-E")
    assert env["OPENCLAW_STATE_DIR"] == str(oc) and env["OPENCLAW_HOME"] == str(staging.HOME)
    assert env["OPENCLAW_SKIP_CRON"] == "1" and env["OPENCLAW_SERVICE_REPAIR_POLICY"] == "external"
    with pytest.raises(SystemExit, match="not found on the staging PATH"):
        staging.unit_argv("x", ["no-such-program"], wait=True)
