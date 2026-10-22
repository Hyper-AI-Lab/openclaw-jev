#!/usr/bin/env python3
"""Tell Kirill that a unit failed: run by rmp-unit-failed@<unit>.service, which the unit's OnFailure= starts.

    venv/bin/python ops/notify_unit_failure.py <unit>

The DM names the unit and the UTC date, with no time of day (the journal has it), then the commands to check the unit.
For rmp-backup.service it also names the run that failed: the newest backup directory, when it is less than 2 h old
(the unit's TimeoutStartSec=1h bounds a run), with the components its manifest lists as failed, or the note that the
run left no manifest. It never quotes a log line.

notify_ops_slack sends a text once per incident id: it keys each DM on the id and the text's sha256. The id holds the
unit and the UTC date, so a unit sends at most one DM a UTC day, and rmp-backup.service one per failed run, whose
stamp and failures the text names. The same alert for the same run is not sent again. send_alert goes to the alert
webhook, which stays off until it is configured.

It exits 0 when the DM went out, or had gone out before, and 1 otherwise, so systemctl --failed shows a lost alert.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import RMP_DATA_DIR  # noqa: E402
from app.production.alerting import send_alert  # noqa: E402
from app.production.backup_manifest import MANIFEST, STAMP  # noqa: E402
from app.production.ops_notify import notify_ops_slack  # noqa: E402

BACKUP_UNIT = "rmp-backup.service"
# The backup unit's TimeoutStartSec=1h bounds a run: a directory younger than this is the run that just failed.
RUN_WINDOW = timedelta(hours=2)


def backup_root() -> Path:
    """Where ops/backup.sh writes its backups, by its own variable and default."""
    return Path(os.environ.get("RMP_BACKUP_ROOT") or os.path.join(RMP_DATA_DIR, "backups"))


def backup_line(root: Path, now: datetime) -> str:
    """What the failed backup run left: the newest directory under ``root`` named like a stamp, when that stamp is
    less than RUN_WINDOW before ``now``. Other directories there, such as openclaw-state/, are never a run."""
    runs = [path for path in root.iterdir() if path.is_dir() and STAMP.fullmatch(path.name)] if root.is_dir() else []
    run = max(runs, key=lambda path: path.name, default=None)
    if run is None or now - datetime.strptime(run.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) >= RUN_WINDOW:
        return "no backup directory from this run"
    try:
        manifest = json.loads((run / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"Backup {run.name}: the run left no manifest (it stopped before the end)"
    failures = manifest.get("failures") if isinstance(manifest, dict) else None
    if isinstance(failures, list) and failures:
        return f"Backup {run.name}: not backed up: {', '.join(map(str, failures))}"
    # The run failed after its manifest was written, recording the off-host copy or pruning.
    return f"Backup {run.name}: its manifest lists no failure (the run failed after writing it)"


def message(unit: str, now: datetime) -> str:
    lines = [f"⚠️ {unit} failed ({now:%Y-%m-%d} UTC)"]
    if unit == BACKUP_UNIT:
        lines.append(backup_line(backup_root(), now))
    lines.append(f"Check: systemctl status {unit}; journalctl -u {unit}")
    return "\n".join(lines)


async def alert(unit: str, text: str, *, incident_id: str) -> bool:
    """Send the DM, then the webhook alert, which goes out even when the DM raised. Whether the DM went out."""
    try:
        delivered = await notify_ops_slack(text, incident_id=incident_id)
    except Exception as exc:
        print(f"the DM failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        delivered = False
    await send_alert("unit.failed", text, severity="error", context={"unit": unit})
    return delivered


def main(argv: Optional[Sequence[str]] = None, *, now: Optional[datetime] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("unit", help="the unit that failed, as rmp-unit-failed@.service's %%i names it")
    args = parser.parse_args(argv)
    now = now or datetime.now(timezone.utc)
    text = message(args.unit, now)
    delivered = asyncio.run(alert(args.unit, text, incident_id=f"unit-failed:{args.unit}:{now:%Y-%m-%d}"))
    print(text)
    print("the DM went out" if delivered else "the DM did not go out")
    return 0 if delivered else 1


if __name__ == "__main__":
    sys.exit(main())
