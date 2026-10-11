"""The database URL comes from the environment or /etc/rmp/rmp.env; the repository holds no database password."""
import os
import re
from pathlib import Path

import pytest

from app.db import database
from tests.production_guard import ALWAYS_DENIED

REPO = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "venv", ".venv", "node_modules", "data", "__pycache__", ".pytest_cache"}
INLINE_URL_PASSWORD = re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^\s:/@'\"`]+:[^\s@'\"`<>]+@")
PGPASSWORD_LITERAL = re.compile(r"PGPASSWORD(?:=|:-)[^\s$\"'{}()`]+")


def test_the_environment_wins(monkeypatch, tmp_path):
    env_file = tmp_path / "rmp.env"
    env_file.write_text("DATABASE_URL=postgresql+asyncpg://rmp@localhost/from_file\n")
    monkeypatch.setattr(database, "RMP_ENV_PATH", env_file)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://rmp@localhost/from_env")
    assert database.resolve_database_url() == "postgresql+asyncpg://rmp@localhost/from_env"


def test_a_unit_without_the_environment_file_reads_it(monkeypatch, tmp_path):
    env_file = tmp_path / "rmp.env"
    env_file.write_text('# RMP\nRMP_API_KEY=k\n\nDATABASE_URL="postgresql+asyncpg://rmp@localhost/from_file"\n')
    monkeypatch.setattr(database, "RMP_ENV_PATH", env_file)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert database.resolve_database_url() == "postgresql+asyncpg://rmp@localhost/from_file"


@pytest.mark.parametrize("content", [None, "RMP_API_KEY=k\n", "DATABASE_URL=\n"])
def test_without_a_url_anywhere_there_is_no_default(monkeypatch, tmp_path, content):
    env_file = tmp_path / "rmp.env"
    if content is not None:
        env_file.write_text(content)
    monkeypatch.setattr(database, "RMP_ENV_PATH", env_file)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="no default database"):
        database.resolve_database_url()


def password_locations(repo):
    """Each path:line under ``repo`` that carries a database password.

    The live state at the repo root is not the repository's: go-live runs the suite in the live checkout, and the
    production guard refuses to read its settings.json and settings.json.lock.
    """
    found = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        if Path(root) == repo:
            files = [name for name in files if name not in ALWAYS_DENIED]
        for name in files:
            path = Path(root) / name
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for pattern in (INLINE_URL_PASSWORD, PGPASSWORD_LITERAL):
                for match in pattern.finditer(text):
                    # The location only: printing the match would print the password.
                    found.append(f"{path.relative_to(repo)}:{text.count(chr(10), 0, match.start()) + 1}")
    return found


def test_no_file_in_the_repository_carries_a_database_password():
    found = password_locations(REPO)
    assert found == [], f"database password in the repository: {found}"


def test_the_password_scan_skips_the_live_state_at_the_repo_root_only(tmp_path):
    url = "postgresql://rmp:" + "pw@localhost/rmp\n"
    for name in ("settings.json", "settings.json.lock", "data/state.json", "app/settings.json", "app/db.py"):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text(url)
    assert sorted(password_locations(tmp_path)) == ["app/db.py:1", "app/settings.json:1"]
