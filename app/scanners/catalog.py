"""Catalog of Moltbook scanner scripts tracked by RMP."""
import glob
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

SEARCH_ROOTS = (
    "/root/aura_safe_harbor",
    "/root/.openclaw/workspace",
)

LOG_OVERRIDES = {
    "moltbook_4hr_swarm": "/root/aura_safe_harbor/moltbook_4hr_run.log",
    "moltbook_swarm_10": "/root/aura_safe_harbor/moltbook_swarm_run.log",
    "real_moltbook_scanner": "/root/.openclaw/workspace/moltbook_research_log.md",
}

DEFAULT_LOG = "/root/.openclaw/workspace/moltbook_research_log.md"


@dataclass(frozen=True)
class ScannerDefinition:
    scanner_id: str
    display_name: str
    script_basename: str
    script_paths: tuple
    log_path: str
    category: str = "moltbook"


def _discover_scanners() -> Dict[str, ScannerDefinition]:
    by_id: Dict[str, List[str]] = {}
    for root in SEARCH_ROOTS:
        for path in glob.glob(f"{root}/moltbook*.js"):
            basename = Path(path).name
            scanner_id = Path(path).stem
            by_id.setdefault(scanner_id, []).append(path)
        for path in glob.glob(f"{root}/real_moltbook_scanner.js"):
            scanner_id = Path(path).stem
            by_id.setdefault(scanner_id, []).append(path)

    catalog: Dict[str, ScannerDefinition] = {}
    for scanner_id, paths in sorted(by_id.items()):
        unique_paths = tuple(sorted(set(paths)))
        log_path = LOG_OVERRIDES.get(scanner_id, DEFAULT_LOG)
        catalog[scanner_id] = ScannerDefinition(
            scanner_id=scanner_id,
            display_name=scanner_id.replace("_", " ").title(),
            script_basename=f"{scanner_id}.js",
            script_paths=unique_paths,
            log_path=log_path,
        )
    return catalog


CATALOG: Dict[str, ScannerDefinition] = _discover_scanners()


def list_scanners() -> List[Dict]:
    return [
        {
            "scanner_id": s.scanner_id,
            "display_name": s.display_name,
            "script_basename": s.script_basename,
            "script_paths": list(s.script_paths),
            "log_path": s.log_path,
            "category": s.category,
        }
        for s in CATALOG.values()
    ]


def get_scanner(scanner_id: str) -> Optional[ScannerDefinition]:
    return CATALOG.get(scanner_id)


def match_scanner_id(cmdline: str) -> Optional[str]:
    """Return scanner_id if cmdline runs a known Moltbook scanner script."""
    lower = (cmdline or "").lower()
    if "node" not in lower and "moltbook" not in lower:
        return None
    for scanner_id, definition in CATALOG.items():
        if definition.script_basename.lower() in lower:
            return scanner_id
    if "real_moltbook_scanner.js" in lower:
        return "real_moltbook_scanner"
    return None
