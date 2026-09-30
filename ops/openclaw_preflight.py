"""Rehearse an OpenClaw upgrade in a scratch directory before anything live is stopped.

Downloads the target with ``npm pack`` and checks what a rehearsal against 2026.9.7 found
broken (2026-09-30): the Node versions it supports, whether ``patch_openclaw.sh`` applies to
its dist, and whether RMP's transcript reader (``app/openclaw_transcripts.py``) can read the way
it stores transcript events: plain ``event_json``, or zstd in ``event_zstd`` with
``event_utf8_bytes``. Exit 0 when the target passes, 1 with the reasons when it does not.
``ops/upgrade_openclaw.sh`` runs it first; it can also be run alone:

    venv/bin/python ops/openclaw_preflight.py [openclaw@latest]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import List, Tuple

RMP_ROOT = Path(__file__).resolve().parent.parent
PATCHER = RMP_ROOT / "patch_openclaw.sh"
TRANSCRIPT_TABLE = "CREATE TABLE IF NOT EXISTS transcript_events"
_COMPARATOR = re.compile(r"^(>=|<=|>|<|=)?v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")
_EVENT_JSON = re.compile(r"\bevent_json\s+TEXT(\s+NOT\s+NULL)?", re.I)
_EVENT_ZSTD = re.compile(r"\bevent_zstd\s+BLOB\b", re.I)
_EVENT_UTF8_BYTES = re.compile(r"\bevent_utf8_bytes\s+INTEGER\b", re.I)
# A definition has columns after the parenthesis; code that locates it in the schema has a quote.
_DEFINITION = re.compile(re.escape(TRANSCRIPT_TABLE) + r"\s*\(\s*[A-Za-z_]")


def _version(text: str) -> Tuple[int, int, int]:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", text.strip())
    if not m:
        raise ValueError(f"not a version: {text!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def satisfies(version: str, spec: str) -> bool:
    """An npm engines range as OpenClaw writes it: ``||`` alternatives of full comparators."""
    have = _version(version)
    for alternative in spec.split("||"):
        parts = alternative.split()
        if not parts:
            continue
        ok = True
        for part in parts:
            m = _COMPARATOR.match(part)
            op = (m.group(1) or "=") if m else ""
            # A partial version pads with zeros only after < and >= (npm: <25 is <25.0.0).
            if not m or (m.group(4) is None and op not in (">=", "<")):
                raise ValueError(f"unsupported range part {part!r}")
            want = (int(m.group(2)), int(m.group(3) or 0), int(m.group(4) or 0))
            ok = ok and {">=": have >= want, "<=": have <= want, ">": have > want,
                         "<": have < want, "=": have == want}[op]
        if ok:
            return True
    return False


def transcript_layouts(dist: Path) -> List[str]:
    """Each transcript table definition's layout: "plain", "zstd" (what RMP decodes) or "unknown"."""
    layouts: List[str] = []
    for path in dist.rglob("*"):
        if path.suffix not in (".js", ".mjs") or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        start = text.find(TRANSCRIPT_TABLE)
        while start >= 0:
            window = text[start:start + 800].replace("\\n", " ").replace("\\t", " ")
            if _DEFINITION.match(window):
                layouts.append(_layout(window))
            start = text.find(TRANSCRIPT_TABLE, start + 1)
    return layouts


def _layout(definition: str) -> str:
    column = _EVENT_JSON.search(definition)
    if column and column.group(1):
        return "plain"
    if column and _EVENT_ZSTD.search(definition) and _EVENT_UTF8_BYTES.search(definition):
        return "zstd"
    return "unknown"


def patches_apply(dist: Path) -> Tuple[bool, str]:
    run = subprocess.run(["bash", str(PATCHER)], env={**os.environ, "OPENCLAW_DIST_DIR": str(dist)},
                         capture_output=True, text=True, timeout=1800)
    errors = [line.strip() for line in run.stdout.splitlines() if line.startswith("ERROR")]
    return run.returncode == 0, "; ".join(errors) or run.stderr.strip()[-300:]


def check(spec: str) -> List[str]:
    """The reasons the target must not be installed; empty when it passes."""
    with tempfile.TemporaryDirectory(prefix="openclaw-preflight-") as tmp:
        packed = subprocess.run(["npm", "pack", spec, "--pack-destination", tmp, "--json"],
                                capture_output=True, text=True, timeout=900)
        if packed.returncode != 0:
            return [f"npm pack {spec} failed: {packed.stderr.strip()[-300:]}"]
        with tarfile.open(Path(tmp) / json.loads(packed.stdout)[0]["filename"]) as tar:
            tar.extractall(tmp, filter="data")
        package = Path(tmp) / "package"
        manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
        print(f"target: {manifest.get('name')}@{manifest.get('version')}")
        problems: List[str] = []
        engines = str((manifest.get("engines") or {}).get("node") or "")
        node = subprocess.run(["node", "--version"], capture_output=True, text=True).stdout.strip()
        try:
            if engines and not satisfies(node, engines):
                problems.append(f"it needs Node {engines}; this host runs {node}")
        except ValueError as exc:
            problems.append(f"its Node range {engines!r} cannot be read ({exc})")
        applied, detail = patches_apply(package / "dist")
        if not applied:
            problems.append(f"RMP's patches do not apply: {detail}")
        layouts = transcript_layouts(package / "dist")
        if not layouts:
            problems.append("no transcript table definition found, so RMP's transcript reads cannot be confirmed")
        elif "unknown" in layouts:
            problems.append("it stores transcript events in a layout RMP's reader does not know "
                            f"(found: {', '.join(sorted(set(layouts)))})")
        return problems


def main(spec: str) -> int:
    problems = check(spec)
    for problem in problems:
        print(f"FAIL: {problem}")
    if not problems:
        print(f"OK: {spec} passes the pre-flight (Node range, RMP patches, transcript format)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "openclaw@latest"))
