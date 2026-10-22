import asyncio
import builtins
import errno
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from temporalio.client import Client

from app.production import backup_manifest as bm
from app.production.readiness import (
    CheckResult,
    check_api_key,
    check_artifact_store,
    check_backups,
    check_development_mode,
    get_last_backup_info,
    run_all_checks,
)

TEMPORAL_UNIT = "/etc/systemd/system/temporal.service"


@pytest.mark.parametrize("env_key, status", [(None, "pass"), ("short", "fail")], ids=["generated", "short"])
def test_api_key_check(env_key, status, tmp_path, monkeypatch):
    import app.config as config

    settings = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", str(settings))
    if env_key is None:
        monkeypatch.delenv("RMP_API_KEY", raising=False)
    else:
        monkeypatch.setenv("RMP_API_KEY", env_key)
    assert check_api_key().status == status
    if env_key is None:
        # Without RMP_API_KEY a key is generated into the settings file.
        assert len(json.loads(settings.read_text())["api_key"]) == 64


@pytest.mark.parametrize("stored, status", [({"development_mode": True}, "warn"), ({}, "pass")], ids=["on", "off"])
def test_development_mode_warns_when_on(stored, status, tmp_path, monkeypatch):
    _stored_settings(tmp_path, monkeypatch, stored)
    assert check_development_mode().status == status


@pytest.mark.asyncio
async def test_run_all_checks_structure(tmp_path, monkeypatch):
    from app.coding import workspace
    from app.production import coding_readiness, readiness as rd
    from app.task_registry import vector_store

    # Sealed: every probe of this host's services, tools and unit files is recorded and refused.
    probes = []

    def no_process(argv, *args, **kwargs):
        probes.append(("process", argv))
        raise FileNotFoundError(errno.ENOENT, "sealed test: no processes", str(argv))

    async def no_async_http(transport, request):
        probes.append(("http", str(request.url)))
        raise httpx.ConnectError("sealed test: no network", request=request)

    def no_http(transport, request):
        probes.append(("http", str(request.url)))
        raise httpx.ConnectError("sealed test: no network", request=request)

    async def no_temporal(target_host, *args, **kwargs):
        probes.append(("temporal", target_host))
        raise RuntimeError("sealed test: no Temporal")

    def no_grpc(host, port, timeout=0.5):
        probes.append(("grpc", (host, port)))
        return False

    real_open = builtins.open

    def no_unit_file(path, *args, **kwargs):
        if str(path) == TEMPORAL_UNIT:
            probes.append(("open", str(path)))
            raise FileNotFoundError(errno.ENOENT, "sealed test: no unit files", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", no_process)
    monkeypatch.setattr(subprocess, "check_output", no_process)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_async_http)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_http)
    monkeypatch.setattr(Client, "connect", no_temporal)
    # gateway_process_pids reads systemctl and /proc.
    monkeypatch.setattr(rd, "gateway_process_pids", lambda: [])
    # The coding checks have their own tests, against an injected host.
    monkeypatch.setattr(coding_readiness, "run_coding_checks", lambda: [CheckResult("coding", "pass", "stubbed")])
    # The Qdrant client's gRPC probe is a raw socket to 6334; with the cached client reset, it runs every time.
    monkeypatch.setattr(vector_store, "_client", None)
    monkeypatch.setattr(vector_store, "_grpc_port_open", no_grpc)
    # Without the password the Postgres check returns before psycopg2, which connects in C where no hook sees it.
    monkeypatch.delenv("TEMPORAL_POSTGRES_PWD", raising=False)
    monkeypatch.setattr(builtins, "open", no_unit_file)
    # The main_deployed invariant asks GitHub with this host's token; tests have none, as CI and coding units.
    monkeypatch.setattr(workspace, "TOKEN_FILE", tmp_path / "github_pat")
    monkeypatch.setattr(rd, "BACKUP_ROOT", str(tmp_path / "backups"))

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
    statuses = {c["name"]: c["status"] for c in result["checks"]}
    # Without a backup the backups check warns, and a warning blocks nothing.
    assert statuses["backups"] == "warn" and "backups" not in result["blocking_failures"]
    # Each probe was refused, and the check that made it reports the service as out of reach.
    assert {name: statuses[name] for name in ("systemd_services", "rmp_health", "temporal", "temporal_persistence",
                                              "task_registry_vector", "stuck_workflows", "host_file_drift")} == {
        "systemd_services": "fail", "rmp_health": "fail", "temporal": "fail", "temporal_persistence": "warn",
        "task_registry_vector": "fail", "stuck_workflows": "warn", "host_file_drift": "warn"}
    assert {kind for kind, _ in probes} == {"process", "http", "temporal", "grpc", "open"}
    assert ("grpc", ("127.0.0.1", 6334)) in probes and ("open", TEMPORAL_UNIT) in probes


@pytest.mark.asyncio
async def test_an_inactive_web_backend_warns_and_an_inactive_core_unit_still_fails(monkeypatch):
    from app.production import readiness as rd

    inactive = set()
    monkeypatch.setattr(rd, "_systemd_active", lambda unit: unit not in inactive)
    r = await rd.check_systemd_services()
    assert (r.status, r.details) == ("pass", {})
    inactive.add("aura-web-backends.service")
    r = await rd.check_systemd_services()
    assert (r.status, r.message) == ("warn", "Inactive units: aura-web-backends.service (web tools unavailable)")
    assert r.details == {"inactive": ["aura-web-backends.service"]}
    inactive.add("rmp-api.service")
    r = await rd.check_systemd_services()
    assert (r.status, r.message) == ("fail", "Inactive units: rmp-api.service")
    assert r.details == {"inactive": ["rmp-api.service", "aura-web-backends.service"]}


def host_files(tmp_path, monkeypatch):
    """A committed unit, its drop-in, the enabled list and two plugin files, each installed as committed, with the
    deploy's unit target and mirror pointed here. Beside them in the target: another unit and its drop-in, a .wants/
    link, a symlinked RMP unit, an RMP unit's backup, and in its drop-in directory a file that is not a .conf and a
    symlinked .conf, none of which is an RMP unit file."""
    from app.coding import deploy

    repo, target, plugins = tmp_path / "repo", tmp_path / "etc-systemd-system", tmp_path / "plugins"
    units = {"rmp-x.service": "[Service]\nExecStart=/bin/true\n", "rmp-x.service.d/10-a.conf": "[Service]\nNice=5\n"}
    files = {**{f"ops/systemd/{name}": text for name, text in units.items()},
             "ops/systemd/enabled-units.txt": "rmp-x.service\n", "plugins/p/a.js": "a\n", "plugins/p/b.js": "b\n"}
    for root, written in ((repo, files), (target, units), (plugins, {"p/a.js": "a\n", "p/b.js": "b\n"})):
        for path, text in written.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text)
            # The committed mode, whatever the umask: RMP's own test runs use 0077.
            (root / path).chmod(0o644)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "units"],
                   check=True)
    (target / "ssh.service").write_text("[Service]\n")
    (target / "ssh.service.d").mkdir()
    (target / "ssh.service.d" / "override.conf").write_text("[Service]\n")
    (target / "multi-user.target.wants").mkdir()
    (target / "multi-user.target.wants" / "rmp-x.service").symlink_to(target / "rmp-x.service")
    (target / "rmp-alias.service").symlink_to(target / "rmp-x.service")
    (target / "rmp-x.service.bak").write_text("[Service]\n")
    (target / "rmp-x.service.d" / "10-a.conf.orig").write_text("[Service]\n")
    (tmp_path / "shared.conf").write_text("[Service]\n")
    (target / "rmp-x.service.d" / "20-shared.conf").symlink_to(tmp_path / "shared.conf")
    monkeypatch.setattr(deploy, "UNIT_TARGET", target)
    monkeypatch.setattr(deploy, "MIRRORS", (("plugins/", plugins),))
    return SimpleNamespace(repo=repo, target=target, plugins=plugins)


def test_host_file_drift_passes_when_every_copy_matches(tmp_path, monkeypatch):
    from app.production.readiness import check_host_file_drift

    host = host_files(tmp_path, monkeypatch)
    r = check_host_file_drift(str(host.repo))
    assert (r.name, r.status, r.message, r.details) == (
        "host_file_drift", "pass", "Installed unit files and mirrors match main", {})


def test_host_file_drift_warns_and_names_each_kind(tmp_path, monkeypatch):
    from app.production.readiness import check_host_file_drift

    host = host_files(tmp_path, monkeypatch)
    (host.target / "rmp-x.service").write_text("[Service]\nExecStart=/bin/false\n")
    (host.target / "rmp-x.service.d" / "10-a.conf").unlink()
    (host.target / "rmp-y.timer").write_text("[Timer]\n")
    (host.target / "aura-z.service.d").mkdir()
    (host.target / "aura-z.service.d" / "x.conf").write_text("[Service]\n")
    (host.plugins / "p" / "a.js").write_text("a, edited\n")
    (host.plugins / "p" / "b.js").chmod(0o600)
    r = check_host_file_drift(str(host.repo))
    assert r.status == "warn"
    assert r.details == {"units": ["rmp-x.service", "rmp-x.service.d/10-a.conf (missing)"],
                         "host_only_units": ["aura-z.service.d/x.conf", "rmp-y.timer"],
                         "mirrors": ["plugins/p/a.js", "plugins/p/b.js"]}
    assert r.message == (
        "Host files differ from main: 2 unit file(s), 2 host-only unit(s), 2 mirror file(s); install units with "
        "ops/install_units.py — rmp-x.service, rmp-x.service.d/10-a.conf (missing), aura-z.service.d/x.conf, "
        "rmp-y.timer, plugins/p/a.js, plugins/p/b.js")


@pytest.mark.parametrize("kind", ["a mask's link to /dev/null", "a dangling link", "a FIFO", "a directory"])
def test_host_file_drift_names_a_tracked_unit_whose_installed_path_is_not_a_regular_file(tmp_path, monkeypatch, kind):
    from app.production.readiness import check_host_file_drift

    host = host_files(tmp_path, monkeypatch)
    unit = host.target / "rmp-x.service"
    unit.unlink()
    if kind == "a mask's link to /dev/null":
        unit.symlink_to("/dev/null")
    elif kind == "a dangling link":
        unit.symlink_to(tmp_path / "nowhere")
    elif kind == "a FIFO":
        os.mkfifo(unit)
    else:
        unit.mkdir()
    # A link to a copy with the committed bytes is no regular file either, and is not read through.
    conf = host.target / "rmp-x.service.d" / "10-a.conf"
    (tmp_path / "10-a.conf").write_bytes(conf.read_bytes())
    conf.unlink()
    conf.symlink_to(tmp_path / "10-a.conf")
    r = check_host_file_drift(str(host.repo))
    assert r.status == "warn"
    assert r.details == {
        "units": ["rmp-x.service (not a regular file)", "rmp-x.service.d/10-a.conf (not a regular file)"],
        "host_only_units": [], "mirrors": []}


@pytest.mark.parametrize("kind", ["a link out of the target", "a dangling link", "a file"])
def test_host_file_drift_names_each_conf_in_a_drop_in_path_that_is_not_a_directory(tmp_path, monkeypatch, kind):
    from app.production.readiness import check_host_file_drift

    host = host_files(tmp_path, monkeypatch)
    shutil.rmtree(host.target / "rmp-x.service.d")
    if kind == "a file":
        (host.target / "rmp-x.service.d").write_text("[Service]\n")
    elif kind == "a dangling link":
        (host.target / "rmp-x.service.d").symlink_to(tmp_path / "nowhere")
    else:
        # The directory the link leads to holds another 10-a.conf; it is never read through the link.
        (tmp_path / "outside").mkdir()
        (tmp_path / "outside" / "10-a.conf").write_text("[Service]\nNice=15\n")
        (host.target / "rmp-x.service.d").symlink_to(tmp_path / "outside")
    r = check_host_file_drift(str(host.repo))
    assert r.status == "warn"
    assert r.details == {"units": ["rmp-x.service.d/10-a.conf (not a regular file)"], "host_only_units": [],
                         "mirrors": []}


def test_host_file_drift_warns_when_it_cannot_compare(tmp_path, monkeypatch):
    from app.production.readiness import check_host_file_drift

    host_files(tmp_path, monkeypatch)
    plain = tmp_path / "plain"
    plain.mkdir()
    # git looks for a repository in the parent directories; none above tmp_path counts.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    r = check_host_file_drift(str(plain))
    assert r.status == "warn" and r.message.startswith("Could not compare host files: git ls-tree")
    assert "not a git repository" in r.message


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
        db.add_all([Task(id=f"p{i}", goal="Also give it in EUR.", status="cancelled",
                         supplementary_context={"intake_reserved": False, "closed_reason": "intake_placeholder"})
                    for i in range(20)])
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


@pytest.fixture
def backups(tmp_path, monkeypatch):
    """BACKUP_ROOT in tmp_path, and the settings there: Qdrant runs as a server."""
    from app.production import readiness as rd

    root = tmp_path / "backups"
    root.mkdir()
    monkeypatch.setattr(rd, "BACKUP_ROOT", str(root))
    _stored_settings(tmp_path, monkeypatch, {"vector_memory": {"qdrant_mode": "server"}})
    return root


def _backup(root, hours_ago, *, qdrant_mode="server", failures=()):
    """A backup that ops/backup.sh started ``hours_ago``, with its format-2 manifest: every component's files, the
    run's ``failures``, and the off-host copy off. rmp_db.dump holds 3 MiB."""
    dest = root / f"{datetime.now(timezone.utc) - timedelta(hours=hours_ago):%Y%m%dT%H%M%SZ}"
    (dest / "openclaw-state").mkdir(parents=True)
    files = {"rmp_db.dump": b"PGDMP" + bytes(3 << 20), "temporal.dump": b"PGDMP temporal",
             "temporal_visibility.dump": b"PGDMP temporal_visibility", "artifacts.tar.gz": b"\x1f\x8b artifacts",
             "workspace.tar.gz": b"\x1f\x8b workspace", "settings.json": b"{}", "openclaw.json": b"{}",
             "openclaw-state/openclaw-agent.sqlite": b"SQLite format 3 agent",
             "openclaw-state/openclaw.sqlite": b"SQLite format 3 state",
             # 20261005T031502Z's Qdrant copy, from before the server snapshot, is a 110-byte qdrant.tar.gz.
             bm.QDRANT_FILES[qdrant_mode]: b"snapshot" if qdrant_mode == "server" else b"\x1f\x8b" + bytes(108)}
    for name, data in files.items():
        (dest / name).write_bytes(data)
    (dest / "openclaw-state" / "manifest.json").write_text(json.dumps(
        {"files": {"agent": {"file": "openclaw-agent.sqlite"}, "state": {"file": "openclaw.sqlite"}}}))
    bm.write(dest, failures=list(failures), temporal_dbs=["temporal", "temporal_visibility"], qdrant_mode=qdrant_mode,
             artifacts_enabled=True, read_back=lambda path: True)
    bm.record_offsite(dest, "off")
    return dest


def _warns(dest, *problems):
    """check_backups' warning for the backup in ``dest``, and the same problems on the dashboard."""
    r = check_backups()
    assert (r.name, r.status, r.message) == ("backups", "warn", f"Backup {dest.name}: {'; '.join(problems)}")
    assert r.details == {"latest": dest.name, "problems": list(problems)}
    info = get_last_backup_info()
    assert (info["found"], info["id"], info["problems"]) == (True, dest.name, list(problems))


def test_a_complete_fresh_backup_passes(backups):
    dest = _backup(backups, 2)
    r = check_backups()
    assert (r.name, r.status, r.message, r.details) == (
        "backups", "pass", f"Backup {dest.name} verified: 7 components, 3 MB, 2.0h old", {"latest": dest.name})
    # The dashboard's keys, with the same backup and its problems.
    taken = datetime.strptime(dest.name, "%Y%m%dT%H%M%SZ")
    assert get_last_backup_info() == {
        "found": True, "id": dest.name, "timestamp": f"{taken:%Y-%m-%dT%H:%M:%SZ}",
        "age_hours": pytest.approx(2, abs=0.01), "manifest": json.loads((dest / "manifest.json").read_text()),
        "path": str(dest), "problems": []}


def test_a_backup_with_manifest_failures_warns(backups):
    """R-127's and R-132's closing test."""
    dest = _backup(backups, 2, failures=["qdrant", "temporal:temporal"])
    _warns(dest, "failed: qdrant, temporal:temporal")


def test_a_legacy_qdrant_tar_in_server_mode_warns(backups, tmp_path, monkeypatch):
    dest = _backup(backups, 2, qdrant_mode="embedded")
    assert json.loads((dest / "manifest.json").read_text())["components"]["qdrant"] == ["qdrant.tar.gz"]
    assert (dest / "qdrant.tar.gz").stat().st_size == 110
    _warns(dest, "Qdrant runs as a server but this backup holds qdrant.tar.gz")
    # The mode is the settings': with Qdrant embedded, the same backup is complete.
    _stored_settings(tmp_path, monkeypatch, {"vector_memory": {"qdrant_mode": "embedded"}})
    assert check_backups().status == "pass"


def test_a_backup_from_before_manifest_format_2_warns(backups):
    """20261005T031502Z's shape: a manifest without failures or a file list, and a 110-byte qdrant.tar.gz. It is made
    fresh here, so its format alone warns."""
    dest = backups / f"{datetime.now(timezone.utc) - timedelta(hours=2):%Y%m%dT%H%M%SZ}"
    dest.mkdir()
    (dest / "qdrant.tar.gz").write_bytes(b"\x1f\x8b" + bytes(108))
    (dest / "manifest.json").write_text(json.dumps({"timestamp": dest.name, "components": ["postgres", "qdrant"]}))
    _warns(dest, "made before manifest format 2 (no file list)")


def test_a_file_that_changed_size_warns(backups):
    dest = _backup(backups, 2)
    size = (dest / "temporal.dump").stat().st_size
    with open(dest / "temporal.dump", "ab") as dump:
        dump.write(b"more")
    _warns(dest, f"temporal.dump is {size + 4} bytes, the manifest says {size}")


def test_a_backup_older_than_26_hours_warns(backups):
    dest = _backup(backups, 27)
    _warns(dest, "27.0h old")
    assert check_backups(max_hours=28).status == "pass"
    _backup(backups, 25)
    assert check_backups().status == "pass"


def test_a_fresher_non_backup_directory_does_not_count(backups):
    """The newest directory with a manifest is no backup unless it is named like a stamp, and the newest stamp is no
    backup until its run wrote the manifest."""
    dest = _backup(backups, 72)
    three_days_ago = (datetime.now(timezone.utc) - timedelta(hours=72)).timestamp()
    os.utime(dest, (three_days_ago, three_days_ago))
    fresh = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    for name in ("openclaw-state", f"openclaw-update-{fresh}"):
        (backups / name).mkdir()
        (backups / name / "manifest.json").write_text("{}")
    (backups / fresh).mkdir()
    _warns(dest, "72.0h old")


def test_a_directory_named_like_a_stamp_that_is_no_date_does_not_break_readiness(backups):
    shutil.copytree(_backup(backups, 2), backups / "20991399T000000Z")
    _warns(backups / "20991399T000000Z", "its name is no date")
    info = get_last_backup_info()
    assert (info["timestamp"], info["age_hours"]) == (None, None)
