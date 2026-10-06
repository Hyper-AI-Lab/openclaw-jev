"""The database URL comes from the environment or /etc/rmp/rmp.env; the repository holds no database password."""
import os
import re
from pathlib import Path

import pytest

from app.db import database

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


def test_no_file_in_the_repository_carries_a_database_password():
    found = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = Path(root) / name
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for pattern in (INLINE_URL_PASSWORD, PGPASSWORD_LITERAL):
                for match in pattern.finditer(text):
                    # The location only: printing the match would print the password.
                    found.append(f"{path.relative_to(REPO)}:{text.count(chr(10), 0, match.start()) + 1}")
    assert found == [], f"database password in the repository: {found}"
