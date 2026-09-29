"""Hermetic host paths for every test, as CI sets them.

app.config resolves these at import time, so they are set before any test imports
app. A local run must never read or write the live settings.json or OpenClaw home.
"""
import os
import tempfile
from pathlib import Path

if "RMP_SETTINGS_PATH" not in os.environ:
    _root = Path(tempfile.mkdtemp(prefix="rmp-tests-"))
    os.environ["OPENCLAW_HOME"] = str(_root / "openclaw")
    os.environ["RMP_ROOT"] = str(Path(__file__).resolve().parents[1])
    os.environ["RMP_DATA_DIR"] = str(_root / "data")
    os.environ["RMP_SETTINGS_PATH"] = str(_root / "settings.json")
    os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_root / 'unmocked.db'}")
    for sub in ("agents/main/agent", "agents/main/sessions", "workspace", "cron"):
        (_root / "openclaw" / sub).mkdir(parents=True, exist_ok=True)
    (_root / "data").mkdir(parents=True, exist_ok=True)
