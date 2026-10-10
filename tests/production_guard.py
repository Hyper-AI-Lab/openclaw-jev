"""The test suite's production guard: which paths no test may touch, wherever the suite runs.

The suite runs in CI, in a reviewed coding job (checkout under ``/srv/aura-code/jobs``, ``TMPDIR`` under
``/srv/aura-code/cache/tmp``), in a direct-session clone (under ``/srv/aura-code/direct``) and from ``make go-live``
(the live checkout ``/root/.openclaw/rmp``). So the production roots are denied, the repo root, the interpreter's
prefixes and the temp dir are allowed even inside them, and the live state a repo root holds in the live checkout is
denied even there. The conftest builds its session guard from here before anything else runs.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import pytest

PathArg = Union[str, bytes, os.PathLike]

# Any access, read or write: the host's production state and secrets, and the live targets of a deploy
# (deploy.MIRRORS writes unit files and Cursor rules; deploy.CODE_RELOAD_LOCK is the host's code-reload lock).
DENIED_ROOTS = (
    "/root/.openclaw",
    "/etc/openclaw",
    "/etc/rmp",
    "/srv/aura-code",
    "/etc/aura-coder",
    "/root/.config",
    "/root/.claude",
    "/root/.claude-team",
    "/etc/systemd/system",
    "/root/.cursor",
    "/run/rmp-code-reload.lock",
)
# Under the repo root, and denied there too: in the live checkout they are production state.
ALWAYS_DENIED = ("data", "settings.json", "settings.json.lock")
# Every environment variable app.config reads a path from.
PATH_VARIABLES = (
    "OPENCLAW_HOME",
    "RMP_ROOT",
    "RMP_DATA_DIR",
    "RMP_SETTINGS_PATH",
    "OPENCLAW_ENV_PATH",
    "AURA_CODE_ROOT",
    "OPENCLAW_CONFIG_PATH",
    "OPENCLAW_SESSIONS_JSON",
    "OPENCLAW_AUTH_PROFILES",
)


def _forms(paths: Iterable[PathArg]) -> Tuple[str, ...]:
    """Each path literally and resolved, once each."""
    forms: Dict[str, None] = {}
    for path in paths:
        literal = os.path.abspath(os.fsdecode(path))
        forms[literal] = None
        forms[os.path.realpath(literal)] = None
    return tuple(forms)


def _under(path: str, roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(root.rstrip(os.sep) + os.sep) for root in roots)


class Guard:
    """Denies a path under a denied root and under no allowed root, and the repo root's live state anywhere.

    The roots are resolved once, here. A checked path is checked literally and resolved, so a symlink out of an
    allowed root into a denied one is denied.
    """

    def __init__(self, denied_roots: Iterable[PathArg], allowed_roots: Iterable[PathArg], repo_root: PathArg):
        self.denied_roots = _forms(denied_roots)
        self.allowed_roots = _forms(allowed_roots)
        self.always_denied = _forms(os.path.join(os.fsdecode(repo_root), name) for name in ALWAYS_DENIED)

    def denies(self, path: PathArg) -> bool:
        literal = os.path.abspath(os.fsdecode(path))
        for form in (literal, os.path.realpath(literal)):
            if _under(form, self.always_denied):
                return True
            if _under(form, self.denied_roots) and not _under(form, self.allowed_roots):
                return True
        return False


def session_guard(
    repo_root: PathArg,
    *,
    prefixes: Sequence[PathArg] = (sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix),
    tempdir: Optional[PathArg] = None,
    denied_roots: Iterable[PathArg] = DENIED_ROOTS,
) -> Guard:
    """The guard of a test session: the repo root, the interpreter's prefixes and the temp dir are allowed.

    An allowed root that contains a denied root (``TMPDIR=/root``, say) would allow it, so it is refused.
    """
    tempdir = tempfile.gettempdir() if tempdir is None else tempdir
    guard = Guard(denied_roots, (repo_root, *prefixes, tempdir), repo_root)
    clashes = [f"{allowed} contains {denied}" for allowed in guard.allowed_roots for denied in guard.denied_roots
               if _under(denied, (allowed,))]
    if clashes:
        raise pytest.UsageError(
            "hermetic tests: an allowed root (the repo root, an interpreter prefix or the temp dir) contains a "
            f"production path: {'; '.join(clashes)}")
    return guard


def production_values(environ: Mapping[str, str], guard: Guard) -> List[Tuple[str, str]]:
    """The path variables in ``environ`` whose value the guard denies, in ``PATH_VARIABLES`` order."""
    return [(name, environ[name]) for name in PATH_VARIABLES if name in environ and guard.denies(environ[name])]


def suite_environment(environ: Mapping[str, str], root: PathArg, repo_root: PathArg) -> Dict[str, str]:
    """What the conftest sets: a default for each path variable ``environ`` lacks, and always the test database.

    Each variable defaults on its own, so an inherited one never decides another's value.
    """
    root = Path(os.fsdecode(root))
    defaults = {
        "OPENCLAW_HOME": root / "openclaw",
        "RMP_ROOT": Path(os.fsdecode(repo_root)),
        "RMP_DATA_DIR": root / "data",
        "RMP_SETTINGS_PATH": root / "settings.json",
        "OPENCLAW_ENV_PATH": root / "openclaw.env",
        "AURA_CODE_ROOT": root / "aura-code",
    }
    environment = {name: str(value) for name, value in defaults.items() if name not in environ}
    # The app's default URL, and the one /etc/rmp/rmp.env exports, is the live database on this host.
    environment["DATABASE_URL"] = f"sqlite+aiosqlite:///{root / 'unmocked.db'}"
    return environment
