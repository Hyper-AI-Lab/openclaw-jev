"""A staging OpenClaw gateway on a copy of production's data, isolated from production.

    venv/bin/python ops/openclaw_staging.py node                     # Node 24 LTS into /opt (checksum verified)
    venv/bin/python ops/openclaw_staging.py build [--version V]      # install, copy data, upgrade steps, patch
    venv/bin/python ops/openclaw_staging.py start|stop|status
    venv/bin/python ops/openclaw_staging.py cli -- ARGS...           # the staging `openclaw` CLI
    venv/bin/python ops/openclaw_staging.py install VERSION          # reinstall a version (rollback rehearsal)
    venv/bin/python ops/openclaw_staging.py restore-pre              # put the pre-migration stores back
    venv/bin/python ops/openclaw_staging.py destroy --yes

How it stays away from production:
- Its own OPENCLAW_HOME, HOME, state directory, config, workspace and base port (19789; production
  uses 18789 and its derived ports reach base + 110).
- Slack off with its tokens removed, cron off (config and OPENCLAW_SKIP_CRON), fresh gateway and
  hook tokens. The RMP plugin's copy calls a closed port, so no hook reaches production RMP.
- Stored production paths in the copied state (agent database lease, workspace, cron store,
  exec-approvals socket, plugin installs) are rewritten to the staging tree.
- Every staging process runs in a transient unit with ProtectSystem=strict and ProtectHome=read-only,
  only /srv/openclaw-staging writable, and no access to systemd or D-Bus: whatever a copied path
  says, it cannot change a production file or restart a production service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ops import backup_openclaw_state as backups  # noqa: E402

RMP_ROOT = Path(__file__).resolve().parent.parent
PROD = Path("/root/.openclaw")
STAGE = Path("/srv/openclaw-staging")
HOME = STAGE / "home"
OC = HOME / ".openclaw"
PREFIX = STAGE / "npm-global"
PACKAGE = PREFIX / "lib" / "node_modules" / "openclaw"
PRE_BACKUP = STAGE / "backup-pre-migration"
NODE_LINK = Path("/opt/node24")
NODE_MAJOR = "v24."
PORT = 19789
UNIT = "openclaw-staging"
DEAD_RMP = "http://127.0.0.1:9"
# OpenClaw's own trees; the RMP repos, logs, media and caches under /root/.openclaw stay behind.
COPY = ["agents", "cron", "workspace", "plugins", "npm", "skills", "plugin-skills", "skill-workshop",
        "devices", "memory", "subagents", "workspace-attestations"]
# Operational records naming production paths; history tables are left as they are.
PATH_TABLES = ["agent_database_leases", "workspace_setup_state", "workspace_path_aliases", "cron_jobs",
               "cron_run_receipts", "cron_job_scratch", "config_health_entries", "exec_approvals_config",
               "config_machine_state", "skill_usage", "plugin_state_entries", "task_runs"]


def log(message: str) -> None:
    print(f"[staging] {message}", flush=True)


def node_bin() -> Path:
    if not (NODE_LINK / "bin" / "node").exists():
        raise SystemExit("Node 24 is not installed; run: openclaw_staging.py node")
    return NODE_LINK / "bin"


def env() -> Dict[str, str]:
    return {
        "HOME": str(HOME), "OPENCLAW_HOME": str(HOME), "OPENCLAW_STATE_DIR": str(OC),
        "OPENCLAW_CONFIG_PATH": str(OC / "openclaw.json"), "OPENCLAW_SKIP_CRON": "1",
        "OPENCLAW_SERVICE_REPAIR_POLICY": "external",
        "PATH": f"{PREFIX / 'bin'}:{node_bin()}:/usr/sbin:/usr/bin:/sbin:/bin",
        "NODE_COMPILE_CACHE": str(STAGE / "cache" / "node-compile"),
        "npm_config_cache": str(STAGE / "cache" / "npm"),
    }


def unit_properties() -> List[str]:
    return [
        "ProtectSystem=strict", "ProtectHome=read-only", f"ReadWritePaths={STAGE}", "PrivateTmp=yes",
        "NoNewPrivileges=yes", "EnvironmentFile=/etc/openclaw/openclaw.env", "MemoryMax=3G",
        "InaccessiblePaths=-/run/systemd/private -/run/dbus -/etc/rmp -/etc/aura-coder -/root/.ssh -/root/.config",
    ]


def unit_argv(unit: str, argv: Sequence[str], *, wait: bool) -> List[str]:
    # systemd looks the command up on its own PATH (production's /usr/bin/openclaw); resolve it here.
    program = shutil.which(argv[0], path=env()["PATH"])
    if not program:
        raise SystemExit(f"{argv[0]} not found on the staging PATH")
    argv = [program, *argv[1:]]
    out = ["systemd-run", f"--unit={unit}", "--collect", "--quiet", f"--working-directory={STAGE}"]
    if wait:
        out += ["--wait", "--pipe"]
    for prop in unit_properties():
        out += ["-p", prop]
    for key, value in env().items():
        out += ["-E", f"{key}={value}"]
    return out + ["--", *argv]


def staged(argv: Sequence[str], *, timeout: int = 1800, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command inside a hardened transient unit and wait for it."""
    name = f"{UNIT}-cmd-{secrets.token_hex(3)}"
    run = subprocess.run(unit_argv(name, argv, wait=True), stdin=subprocess.DEVNULL, capture_output=True,
                         text=True, timeout=timeout)
    if check and run.returncode != 0:
        raise SystemExit(f"staged command failed ({run.returncode}): {' '.join(argv)}\n{run.stdout[-3000:]}\n{run.stderr[-3000:]}")
    return run


def install_node() -> Path:
    with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=60) as resp:
        releases = json.load(resp)
    release = next(r for r in releases if r["version"].startswith(NODE_MAJOR) and r["lts"])
    version = release["version"]
    name = f"node-{version}-linux-x64"
    target = Path("/opt") / name
    if not (target / "bin" / "node").exists():
        base = f"https://nodejs.org/dist/{version}"
        with urllib.request.urlopen(f"{base}/SHASUMS256.txt", timeout=60) as resp:
            sums = resp.read().decode()
        expected = next(line.split()[0] for line in sums.splitlines() if line.endswith(f"  {name}.tar.xz"))
        tarball = Path("/opt") / f"{name}.tar.xz"
        log(f"downloading {name}.tar.xz")
        urllib.request.urlretrieve(f"{base}/{name}.tar.xz", tarball)
        digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
        if digest != expected:
            tarball.unlink()
            raise SystemExit(f"checksum mismatch for {tarball.name}: {digest} != {expected}")
        log(f"sha256 ok ({digest[:16]}…)")
        with tarfile.open(tarball) as tar:
            tar.extractall("/opt", filter="data")
        tarball.unlink()
    if NODE_LINK.is_symlink() or NODE_LINK.exists():
        NODE_LINK.unlink()
    NODE_LINK.symlink_to(target)
    out = subprocess.run([str(target / "bin" / "node"), "--version"], capture_output=True, text=True).stdout.strip()
    log(f"Node {out} at {target} (link {NODE_LINK}); not on PATH")
    return target


def install_openclaw(version: str) -> str:
    PREFIX.mkdir(parents=True, exist_ok=True)
    npm = ["npm", "install", "-g", "--prefix", str(PREFIX), f"openclaw@{version}"]
    if staged(npm, timeout=1800, check=False).returncode != 0:
        staged([*npm, "--allow-scripts=openclaw"], timeout=1800)
    installed = json.loads((PACKAGE / "package.json").read_text())["version"]
    log(f"openclaw {installed} installed under {PREFIX}")
    patch = subprocess.run(["bash", str(RMP_ROOT / "patch_openclaw.sh")], capture_output=True, text=True,
                           env={**os.environ, "OPENCLAW_DIST_DIR": str(PACKAGE / "dist")}, timeout=1800)
    tail = [line for line in patch.stdout.splitlines() if line.startswith(("OK:", "ERROR", "Done."))]
    log("patch: " + " | ".join(tail))
    if patch.returncode != 0:
        raise SystemExit("RMP patches did not apply to the staging dist")
    return installed


def copy_data() -> None:
    OC.mkdir(parents=True, exist_ok=True)
    for name in COPY:
        src = PROD / name
        if not src.exists():
            continue
        dest = OC / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, symlinks=True,
                        ignore=shutil.ignore_patterns("openclaw-agent.sqlite*", "*.sqlite-wal", "*.sqlite-shm"))
    (OC / "logs").mkdir(exist_ok=True)
    (OC / "state").mkdir(exist_ok=True)
    if PRE_BACKUP.exists():
        shutil.rmtree(PRE_BACKUP)
    manifest = backups.backup(PRE_BACKUP, home=PROD)
    for meta in manifest["files"].values():
        target = OC / Path(meta["source"]).relative_to(PROD)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PRE_BACKUP / meta["file"], target)
        os.chmod(target, 0o600)
    sizes = ", ".join(f"{n} {m['bytes'] >> 20} MiB" for n, m in manifest["files"].items())
    log(f"copied {', '.join(COPY)}; stores through the backup API ({sizes}); pre-migration copy at {PRE_BACKUP}")


def rewrite_state_paths() -> int:
    con = sqlite3.connect(OC / "state" / "openclaw.sqlite")
    changed = 0
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in PATH_TABLES:
            if table not in tables:
                continue
            for column in [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]:
                cur = con.execute(
                    f'UPDATE "{table}" SET "{column}" = REPLACE("{column}", ?, ?) '
                    f'WHERE typeof("{column}") = \'text\' AND "{column}" LIKE ?',
                    (str(PROD), str(OC), f"%{PROD}%"))
                changed += cur.rowcount
        # The copy's leases belong to production's live gateway; the upgrade opens the stores stopped.
        if "agent_database_leases" in tables:
            con.execute("DELETE FROM agent_database_leases")
        con.commit()
        left = [t for t in PATH_TABLES if t in tables for c in [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
                if con.execute(f'SELECT COUNT(*) FROM "{t}" WHERE CAST("{c}" AS TEXT) LIKE ?', (f"%{PROD}%",)).fetchone()[0]]
    finally:
        con.close()
    if left:
        raise SystemExit(f"production paths left in {left}")
    log(f"rewrote {changed} stored production paths to {OC}")
    return changed


def write_config() -> Dict[str, str]:
    config = json.loads((PROD / "openclaw.json").read_text())
    tokens = {"gateway": secrets.token_hex(24), "hooks": secrets.token_hex(24)}
    config["gateway"]["port"] = PORT
    config["gateway"]["auth"]["token"] = tokens["gateway"]
    config["hooks"]["token"] = tokens["hooks"]
    slack = config.get("channels", {}).get("slack", {})
    slack["enabled"] = False
    for key in ("botToken", "appToken", "userToken", "signingSecret"):
        slack.pop(key, None)
    config.setdefault("plugins", {}).setdefault("entries", {}).setdefault("slack", {})["enabled"] = False
    config["cron"] = {**(config.get("cron") or {}), "enabled": False}
    text = json.dumps(config, indent=2).replace(str(PROD), str(OC))
    target = OC / "openclaw.json"
    target.write_text(text + "\n")
    os.chmod(target, 0o600)
    if str(PROD) in target.read_text():
        raise SystemExit("production paths left in the staging config")
    log(f"config: port {PORT}, Slack off without tokens, cron off, fresh gateway and hook tokens")
    return tokens


def isolate_rmp_plugin() -> None:
    index = OC / "plugins" / "rmp_adapter" / "index.js"
    text = index.read_text()
    new = text.replace("const RMP_API = 'http://127.0.0.1:8000';", f"const RMP_API = '{DEAD_RMP}';")
    new = new.replace("const LOG = '/root/.openclaw/logs/rmp_adapter.log';", f"const LOG = '{OC}/logs/rmp_adapter.log';")
    if new.count(DEAD_RMP) != 1 or "/root/.openclaw/logs/rmp_adapter.log" in new:
        raise SystemExit("could not isolate the RMP plugin copy (its constants moved)")
    index.write_text(new)
    log(f"RMP plugin copy calls {DEAD_RMP} and logs under {OC / 'logs'}")


def save_upgrade_backup(config_path: Path) -> Path:
    """What ops/upgrade_openclaw.sh keeps for a rollback, for ROLLBACK_TARGET=staging."""
    dest = STAGE / "upgrade-backup"
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "plugins").mkdir(parents=True)
    (dest / "workspace").mkdir()
    shutil.copyfile(config_path, dest / "openclaw.json")
    for name in ("rmp_adapter", "aura_web"):
        if (OC / "plugins" / name).is_dir():
            shutil.copytree(OC / "plugins" / name, dest / "plugins" / name, symlinks=True)
    for name in ("TOOLS.md", "AGENTS.md"):
        if (PROD / "workspace" / name).is_file():
            shutil.copyfile(PROD / "workspace" / name, dest / "workspace" / name)
    before = subprocess.run(["openclaw", "--version"], capture_output=True, text=True, timeout=120).stdout.strip()
    specs = [f"plugin_before={json.loads(p.read_text())['name']}@{json.loads(p.read_text())['version']}"
             for p in sorted(PROD.glob("npm/projects/*/node_modules/@openclaw/*/package.json"))]
    (dest / "VERSIONS.txt").write_text("\n".join([f"openclaw_before={before.splitlines()[0]}", *specs]) + "\n")
    log(f"upgrade backup for the rollback rehearsal at {dest} ({before.splitlines()[0]}, {len(specs)} plugins)")
    return dest


def after_doctor() -> None:
    """The upgrade's restore_tools_md and ensure_openclaw_skills.sh, on the staging tree."""
    tools = OC / "workspace" / "TOOLS.md"
    if not tools.exists():
        shutil.copyfile(PROD / "workspace" / "TOOLS.md", tools)
        log("TOOLS.md restored after doctor archived it")
    skills = PACKAGE / "skills"
    skills.mkdir(exist_ok=True)
    for skill in sorted((OC / "skills").iterdir()) if (OC / "skills").is_dir() else []:
        link = skills / skill.name
        if not link.exists():
            link.symlink_to(skill)
            log(f"linked skill {skill.name}")


def build(version: str) -> None:
    if unit_active(UNIT):
        raise SystemExit("stop the staging gateway first")
    STAGE.mkdir(parents=True, exist_ok=True)
    os.chmod(STAGE, 0o700)
    started = time.monotonic()
    install_openclaw(version)
    copy_data()
    rewrite_state_paths()
    write_config()
    isolate_rmp_plugin()
    save_upgrade_backup(OC / "openclaw.json")
    # The production upgrade's steps, in its order (ops/upgrade_openclaw.sh).
    run = staged(["openclaw", "plugins", "update", "--all"], timeout=1800, check=False)
    log(f"plugins update --all: exit {run.returncode}; " + " | ".join(run.stdout.strip().splitlines()[-4:]))
    run = staged(["openclaw", "doctor", "--fix", "--non-interactive"], timeout=1800, check=False)
    log(f"doctor --fix: exit {run.returncode}; " + " | ".join(run.stdout.strip().splitlines()[-4:]))
    after_doctor()
    settle = subprocess.run([str(RMP_ROOT / "venv" / "bin" / "python"), str(RMP_ROOT / "ops" / "settle_openclaw_sessions.py"),
                             str(OC / "agents" / "main" / "agent" / "openclaw-agent.sqlite")],
                            capture_output=True, text=True, timeout=600)
    log(f"settle: exit {settle.returncode}; {settle.stdout.strip()[-300:]}")
    if (PACKAGE / "dist").is_dir():
        audit = subprocess.run([sys.executable, str(RMP_ROOT / "ops" / "openclaw_patch_audit.py"), str(PACKAGE / "dist")],
                               capture_output=True, text=True)
        log(audit.stdout.strip().splitlines()[-1] if audit.stdout.strip() else "audit printed nothing")
    log(f"build done in {time.monotonic() - started:.0f} s")


def unit_active(unit: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0


def start(timeout: int = 900) -> float:
    if unit_active(UNIT):
        raise SystemExit("the staging gateway is already running")
    started = time.monotonic()
    subprocess.run(unit_argv(UNIT, ["openclaw", "gateway", "--port", str(PORT)], wait=False), check=True)
    ready = wait_ready(started, timeout)
    log(f"ready after {ready:.1f} s")
    return ready


def wait_ready(started: float, timeout: int) -> float:
    last = ""
    while time.monotonic() - started < timeout:
        if not unit_active(UNIT):
            raise SystemExit(f"the staging gateway exited; see: journalctl -u {UNIT}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/readyz", timeout=5) as resp:
                if resp.status == 200:
                    return time.monotonic() - started
        except Exception as exc:  # noqa: BLE001 - polling until ready
            last = str(exc)
        time.sleep(1)
    raise SystemExit(f"not ready after {timeout} s ({last})")


def stop() -> None:
    subprocess.run(["systemctl", "stop", UNIT], check=False)
    log("stopped")


def restore_pre() -> None:
    if unit_active(UNIT):
        raise SystemExit("stop the staging gateway first")
    manifest = backups.verify(PRE_BACKUP)
    for meta in manifest["files"].values():
        target = OC / Path(meta["source"]).relative_to(PROD)
        for suffix in ("-wal", "-shm"):
            target.with_name(target.name + suffix).unlink(missing_ok=True)
        shutil.copyfile(PRE_BACKUP / meta["file"], target)
        os.chmod(target, 0o600)
    rewrite_state_paths()
    log(f"restored the pre-migration stores ({', '.join(manifest['files'])}, taken {manifest['created_at']})")


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("node")
    b = sub.add_parser("build")
    b.add_argument("--version", default="2026.9.7")
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("status")
    c = sub.add_parser("cli")
    c.add_argument("args", nargs=argparse.REMAINDER)
    i = sub.add_parser("install")
    i.add_argument("version")
    sub.add_parser("restore-pre")
    d = sub.add_parser("destroy")
    d.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "node":
        install_node()
    elif args.action == "build":
        build(args.version)
    elif args.action == "start":
        start()
    elif args.action == "stop":
        stop()
    elif args.action == "status":
        print(json.dumps({"active": unit_active(UNIT), "port": PORT, "home": str(OC),
                          "version": json.loads((PACKAGE / "package.json").read_text())["version"]
                          if (PACKAGE / "package.json").exists() else None}))
    elif args.action == "cli":
        rest = args.args[1:] if args.args[:1] == ["--"] else args.args
        run = staged(["openclaw", *rest], check=False)
        sys.stdout.write(run.stdout)
        sys.stderr.write(run.stderr)
        return run.returncode
    elif args.action == "install":
        if unit_active(UNIT):
            raise SystemExit("stop the staging gateway first")
        install_openclaw(args.version)
    elif args.action == "restore-pre":
        restore_pre()
    elif args.action == "destroy":
        if not args.yes:
            print("refusing without --yes", file=sys.stderr)
            return 2
        stop()
        shutil.rmtree(STAGE, ignore_errors=True)
        log(f"removed {STAGE}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
