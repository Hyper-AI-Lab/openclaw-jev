"""The coding runner's host layer: hardened units, the aura-coder firewall, and the stored token."""
import json
import stat
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.coding import credentials, firewall, units
from app.config import DEFAULT_CODING, load_settings

TOKEN = "sk-ant-oat01-" + "Ab3_-" * 19


def test_every_coding_unit_is_hardened_limited_and_blind_to_secrets():
    props = units.unit_properties(writable=["/srv/aura-code/jobs/t1", units.CODER_HOME], memory_max="3G",
                                  cpu_quota="250%", tasks_max=1024, runtime_max_sec=5400,
                                  env_file=units.TOKEN_ENV_FILE)
    for required in ("NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectSystem=strict", "ProtectHome=read-only",
                     "CapabilityBoundingSet=", "RestrictSUIDSGID=yes", "MemoryMax=3G", "MemorySwapMax=0",
                     "TasksMax=1024", "CPUQuota=250%", "RuntimeMaxSec=5400", "CPUWeight=50"):
        assert required in props
    hidden = next(p for p in props if p.startswith("InaccessiblePaths="))
    for path in ("/root", "/etc/aura-coder", "/etc/rmp", "/etc/openclaw", "/var/run/docker.sock"):
        assert f"-{path}" in hidden.split("=", 1)[1].split()
    assert "ReadWritePaths=/srv/aura-code/jobs/t1 /home/aura-coder" in props
    assert "EnvironmentFile=/etc/aura-coder/claude.env" in props
    assert not any(p.startswith("EnvironmentFile=") for p in units.unit_properties(
        writable=["/tmp/x"], memory_max="1G", cpu_quota="100%", tasks_max=10, runtime_max_sec=60))


def test_systemd_run_starts_a_collected_unit_as_aura_coder():
    argv = units.systemd_run_argv("aura-claude-t1-1", ["claude", "-p", "hi"], properties=["MemoryMax=1G"],
                                  workdir="/srv/aura-code/jobs/t1/repo", env={"FOO": "bar"}, wait=True, pipe=True)
    assert argv[:4] == ["systemd-run", "--unit=aura-claude-t1-1", "--uid=aura-coder", "--gid=aura-coder"]
    for flag in ("--collect", "--wait", "--pipe", "--working-directory=/srv/aura-code/jobs/t1/repo",
                 "--setenv=HOME=/home/aura-coder", "--setenv=FOO=bar", "--property=MemoryMax=1G"):
        assert flag in argv
    assert argv[argv.index("--"):] == ["--", "claude", "-p", "hi"]
    assert "--wait" not in units.systemd_run_argv("u", ["x"], properties=[], workdir="/tmp")


@pytest.mark.parametrize("bad", [0, 70000, "a-b", "10-5", "5-5", "", "22,23"])
def test_a_bad_port_is_refused_rather_than_silently_dropped(bad):
    with pytest.raises(ValueError):
        firewall.port_elements([bad])


def test_the_ruleset_replaces_itself_and_blocks_services_and_private_networks():
    rules = firewall.render([22, "7233-7243", 8000])
    assert rules.startswith("add table inet aura_coder\ndelete table inet aura_coder\n")
    assert "elements = { 22, 7233-7243, 8000 }" in rules
    assert 'meta skuid "aura-coder" oifname "lo" tcp dport @local_ports counter reject with tcp reset' in rules
    assert "169.254.0.0/16" in rules and "172.16.0.0/12" in rules and "fc00::/7" in rules
    elements = firewall.port_elements(DEFAULT_CODING["blocked_tcp_ports"])
    for port in (22, 5432, 6333, 7233, 7243, 8000, 8791, 9222, 18789, 18791):
        assert firewall.covers(elements, port)
    assert not firewall.covers(elements, 53)


def test_listeners_the_rules_leave_open_are_reported(monkeypatch):
    ss = "\n".join([
        "LISTEN 0 4096 127.0.0.1:8000 0.0.0.0:*",
        "LISTEN 0 4096 127.0.0.1:5000 0.0.0.0:*",
        "LISTEN 0 4096 0.0.0.0:3001 0.0.0.0:*",
        "LISTEN 0 4096 [::1]:4444 [::]:*",
        "LISTEN 0 4096 127.0.0.1:40123 0.0.0.0:*",
        "LISTEN 0 4096 10.0.0.5:3000 0.0.0.0:*",
        "LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*",
    ])
    monkeypatch.setattr(firewall.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=ss, returncode=0))
    assert firewall.uncovered_listeners([8000]) == ["0.0.0.0:3001", "127.0.0.1:5000", "[::1]:4444"]
    assert firewall.uncovered_listeners([8000, 3001, 4444, 5000]) == []


def test_the_token_is_found_in_a_terminal_transcript_and_rejoined_when_wrapped():
    plain = f"\x1b[1mLong-lived token created\x1b[0m\r\n\r\n  {TOKEN}\r\n\r\nStore it securely.\r\n"
    assert credentials.extract_token(plain) == TOKEN
    wrapped = f"Your token:\r\n{TOKEN[:80]}\r\n{TOKEN[80:]}\r\nDone\r\n"
    assert credentials.extract_token(wrapped) == TOKEN
    followed = f"{TOKEN}\nDone\n"
    assert credentials.extract_token(followed) == TOKEN
    assert credentials.extract_token("Error: authorization cancelled\n") is None
    assert credentials.is_valid(TOKEN)
    assert not credentials.is_valid(TOKEN[:40]) and not credentials.is_valid("sk-ant-api03-" + "x" * 90)


def test_the_token_is_stored_for_systemd_only_with_metadata_but_no_secret(tmp_path):
    env_file, meta_file = tmp_path / "secrets" / "claude.env", tmp_path / "secrets" / "claude-token.json"
    meta = credentials.write_token(TOKEN, env_file, meta_file, now=datetime(2026, 9, 30, 12, tzinfo=timezone.utc))
    assert env_file.read_text() == f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600 and stat.S_IMODE(meta_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_file.parent.stat().st_mode) == 0o700
    assert meta == {"issued_at": "2026-09-30T12:00:00+00:00", "expires_at": "2027-09-30T12:00:00+00:00",
                    "length": len(TOKEN), "fingerprint": credentials.fingerprint(TOKEN)}
    assert TOKEN not in meta_file.read_text() and json.loads(meta_file.read_text()) == meta
    assert credentials.read_meta(meta_file) == meta
    with pytest.raises(ValueError):
        credentials.write_token("not-a-token", env_file, meta_file)


def test_coding_settings_merge_over_their_defaults():
    coding = load_settings()["coding"]
    assert set(DEFAULT_CODING) <= set(coding)
    assert coding["claude_version"].count(".") == 2 and coding["model"] and coding["fallback_model"]


def test_the_ops_scripts_parse():
    for script in ("ops/setup_aura_coder.sh", "ops/claude_code_login.sh", "ops/aura_coder_firewall.sh"):
        assert subprocess.run(["bash", "-n", script]).returncode == 0
    managed = json.loads(open("ops/aura_coder/managed-settings.json").read())
    assert managed["env"]["DISABLE_UPDATES"] == "1" and managed["allowedMcpServers"] == []
    assert "Bash(git push *)" in managed["permissions"]["deny"]
    assert "disableBypassPermissionsMode" not in managed.get("permissions", {})
