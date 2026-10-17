"""The tracked unit directory, ops/systemd/: every RMP unit of this host has its file there and nowhere else, each file
has its sections, every timer is enabled, and no code names a retired unit. Reads repo files only, never git."""
import os
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
UNIT_DIR = REPO / "ops" / "systemd"
ENABLED = "enabled-units.txt"
UNIT_SUFFIXES = (".service", ".timer", ".path", ".socket")
TYPE_SECTIONS = {".service": "Service", ".timer": "Timer", ".path": "Path"}
RETIRED = ("temporal-dev", "aura-web-stack.service")
# The units that sync the LLM keys into OpenClaw's auth store before each start.
KEY_SYNC_UNITS = ("openclaw-gateway.service", "rmp-api.service", "rmp-worker.service")
# Runtime content that .gitignore keeps out of the repo.
RUNTIME_DIRS = {"__pycache__", "node_modules", ".venv", "venv"}

# The host's unit files on 2026-10-15, from systemctl list-unit-files 'rmp-*' 'aura-*' 'openclaw*' 'temporal*', plus
# openclaw-gateway.service.d/50-oompolicy.conf, less temporal-dev.service and aura-web-stack.service, which WP-02 retires.
INVENTORY = {
    "aura-coder-firewall.service",
    "aura-obscura.service",
    "aura-web-backends.service",
    "openclaw-gateway.service",
    "openclaw-gateway.service.d/50-oompolicy.conf",
    "rmp-api.service",
    "rmp-backup.service",
    "rmp-backup.timer",
    "rmp-canary-sentinel.service",
    "rmp-canary-sentinel.timer",
    "rmp-canary.service",
    "rmp-canary.timer",
    "rmp-code-reload.path",
    "rmp-code-reload.service",
    "rmp-code-watch.service",
    "rmp-janitor-frequent.service",
    "rmp-janitor-frequent.timer",
    "rmp-janitor.service",
    "rmp-janitor.timer",
    "rmp-memory-canary.service",
    "rmp-memory-canary.timer",
    "rmp-observability.service",
    "rmp-qdrant.service",
    "rmp-temporal-vacuum.service",
    "rmp-temporal-vacuum.timer",
    "rmp-temporal-watchdog.service",
    "rmp-temporal-watchdog.timer",
    "rmp-worker.service",
    "temporal.service",
}


def _files_under(top: Path, skip: Optional[Path] = None) -> Iterator[Path]:
    for root, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if d not in RUNTIME_DIRS and Path(root, d) != skip]
        yield from (Path(root, name) for name in files)


def _unit_files() -> List[str]:
    """Every file under ops/systemd/ but the enabled list, as a name relative to it."""
    names = (path.relative_to(UNIT_DIR).as_posix() for path in _files_under(UNIT_DIR))
    return sorted(name for name in names if name != ENABLED)


def _enabled_units() -> List[str]:
    lines = (line.strip() for line in (UNIT_DIR / ENABLED).read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _sections(name: str) -> Dict[str, List[Tuple[str, str]]]:
    """A unit file's sections, each with its (key, value) lines in order. systemd repeats keys, so not configparser."""
    sections: Dict[str, List[Tuple[str, str]]] = {}
    current = None
    for line in (UNIT_DIR / name).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], [])
        elif current is not None and "=" in line:
            key, value = line.split("=", 1)
            current.append((key.strip(), value.strip()))
    return sections


def _is_drop_in(name: str) -> bool:
    return Path(name).parent.name.endswith(".d")


def test_every_live_rmp_unit_has_its_file_in_ops_systemd():
    assert set(_unit_files()) == INVENTORY


def test_the_repo_keeps_unit_files_only_in_ops_systemd():
    places = [path for top in ("app", "plugins", "web-stack") for path in _files_under(REPO / top)]
    places += _files_under(REPO / "ops", skip=UNIT_DIR)
    places += [path for path in REPO.iterdir() if path.is_file()]
    assert [str(path.relative_to(REPO)) for path in places if path.name.endswith(UNIT_SUFFIXES)] == []
    assert not (REPO / "systemd").exists()


def test_every_unit_file_has_its_sections():
    missing = []
    for name in _unit_files():
        sections = _sections(name)
        if _is_drop_in(name):
            if not sections:
                missing.append(f"{name}: no section")
            continue
        wanted = ["Unit"] + [section for suffix, section in TYPE_SECTIONS.items() if name.endswith(suffix)]
        missing += [f"{name}: [{section}]" for section in wanted if section not in sections]
    assert missing == []


def test_every_timer_starts_a_tracked_service_and_is_enabled():
    files = set(_unit_files())
    enabled = _enabled_units()
    problems = []
    for name in sorted(files):
        if _is_drop_in(name) or not name.endswith((".timer", ".path")):
            continue
        section = "Timer" if name.endswith(".timer") else "Path"
        started = dict(_sections(name).get(section, [])).get("Unit", f"{Path(name).stem}.service")
        if not started.endswith(".service") or started not in files:
            problems.append(f"{name} starts {started}, which is not a tracked service")
        if name.endswith(".timer") and name not in enabled:
            problems.append(f"{name} is not in {ENABLED}")
    for unit in enabled:
        if unit not in files:
            problems.append(f"{unit} is listed but has no file")
        elif "Install" not in _sections(unit):
            problems.append(f"{unit} is listed but has no [Install] section, so it is static and cannot be enabled")
    assert problems == []


def test_no_key_sync_or_patch_audit_can_block_a_start():
    """A failed key sync is logged and never stops a unit (B4b-02); a start-time patch audit could not stop one either."""
    blocking, syncs = [], {}
    for name in _unit_files():
        unit = Path(name).parent.name[:-2] if _is_drop_in(name) else name
        for key, value in _sections(name).get("Service", []):
            if key != "ExecStartPre" or not ("sync_nvidia_keys.py" in value or "openclaw_patch_audit.py" in value):
                continue
            if not value.startswith("-"):
                blocking.append(f"{name}: ExecStartPre={value}")
            if "sync_nvidia_keys.py" in value:
                syncs[unit] = syncs.get(unit, 0) + 1
    assert blocking == []
    assert {unit: syncs.get(unit, 0) for unit in KEY_SYNC_UNITS} == dict.fromkeys(KEY_SYNC_UNITS, 1)


def test_no_code_names_a_retired_unit():
    # A retired unit's own file names it only in its path (temporal-dev.service never says temporal-dev inside).
    hits = []
    for path in (path for top in ("app", "ops", "web-stack") for path in _files_under(REPO / top)):
        where, data = path.relative_to(REPO).as_posix(), path.read_bytes()
        hits += [f"{where}: {name}" for name in RETIRED if name in where or name.encode() in data]
    assert hits == []
