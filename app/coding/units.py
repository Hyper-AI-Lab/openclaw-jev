"""Transient systemd units that run work as aura-coder, hardened the same way every time.

Claude Code's own permission checks are not a boundary in headless mode (claude-code#33343),
so the boundary is the operating system: an unprivileged user, a read-only system, writes only
to the job directory and its home, secrets hidden, resource limits below the production
services, and a firewall that keeps it off this host's services (``app/coding/firewall.py``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

CODER_USER = "aura-coder"
CODER_HOME = Path("/home/aura-coder")
CODE_ROOT = Path("/srv/aura-code")
JOBS_DIR = CODE_ROOT / "jobs"
RUNS_DIR = CODE_ROOT / "runs"
CACHE_DIR = CODE_ROOT / "cache"
VENVS_DIR = CODE_ROOT / "venvs"
CLAUDE_BIN = CODER_HOME / ".local" / "bin" / "claude"
SECRETS_DIR = Path("/etc/aura-coder")
TOKEN_ENV_FILE = SECRETS_DIR / "claude.env"
TOKEN_META_FILE = SECRETS_DIR / "claude-token.json"
MANAGED_SETTINGS = Path("/etc/claude-code/managed-settings.json")
UNIT_PREFIX = "aura-claude-"
# Hidden from every coding unit whatever their file modes; missing ones are skipped.
# Sockets are named by their /run paths: /var/run is a symlink to /run.
HIDDEN_PATHS = (
    "/root",
    str(SECRETS_DIR),
    "/etc/rmp",
    "/etc/openclaw",
    "/var/lib/postgresql",
    "/run/postgresql",
    "/run/dbus",
    "/run/docker.sock",
    "/run/containerd",
    "/run/snapd.socket",
    "/run/snapd-snap.socket",
    "/run/lxd-installer.socket",
    "/run/user",
)
BASE_PATH = f"{CODER_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"

PathLike = Union[str, Path]


def unit_properties(
    *,
    writable: Sequence[PathLike],
    memory_max: str,
    cpu_quota: str,
    tasks_max: int,
    runtime_max_sec: int,
    env_file: Optional[PathLike] = None,
) -> List[str]:
    """systemd properties for a coding unit; ``writable`` must exist."""
    props = [
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "ProtectClock=yes",
        "ProtectHostname=yes",
        # Other users' command lines carry secrets (Temporal's container gets its Postgres password as an argument).
        "ProtectProc=invisible",
        "RestrictSUIDSGID=yes",
        "RestrictRealtime=yes",
        "LockPersonality=yes",
        "SystemCallArchitectures=native",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "UMask=0077",
        f"MemoryMax={memory_max}",
        "MemorySwapMax=0",
        f"TasksMax={tasks_max}",
        f"CPUQuota={cpu_quota}",
        "CPUWeight=50",
        "IOWeight=50",
        "Nice=5",
        f"RuntimeMaxSec={runtime_max_sec}",
        "InaccessiblePaths=" + " ".join(f"-{p}" for p in HIDDEN_PATHS),
        "ReadWritePaths=" + " ".join(str(p) for p in writable),
    ]
    if env_file:
        # systemd reads it as root before dropping to the user, who cannot read the file.
        props.append(f"EnvironmentFile={env_file}")
    return props


def systemd_run_argv(
    unit: str,
    command: Sequence[str],
    *,
    properties: Sequence[str],
    workdir: PathLike,
    env: Optional[Dict[str, str]] = None,
    wait: bool = False,
    pipe: bool = False,
) -> List[str]:
    """``systemd-run`` for a transient service as aura-coder; ``--collect`` unloads it after exit."""
    argv = [
        "systemd-run",
        f"--unit={unit}",
        f"--uid={CODER_USER}",
        f"--gid={CODER_USER}",
        f"--working-directory={workdir}",
        "--collect",
        "--quiet",
    ]
    if wait:
        argv.append("--wait")
    if pipe:
        argv.append("--pipe")
    environment = {"HOME": str(CODER_HOME), "PATH": BASE_PATH, "LANG": "C.UTF-8", **(env or {})}
    argv += [f"--setenv={key}={value}" for key, value in environment.items()]
    argv += [f"--property={prop}" for prop in properties]
    return [*argv, "--", *command]
