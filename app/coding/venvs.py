"""Python venvs built from a repository's lock files, one per lock, and the link that serves one.

RMP's deploy builds the venv of the head's lock files before the change and points the live checkout's ``venv``, a
symlink, at it while the units are stopped; the coding jobs' shared venv is built the same way. A venv records its own
path in its scripts, so a built venv is never moved: it is built in place, and its stamp, the lock hash written last,
marks it complete.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, List, Optional, Union

from app.config import OPENCLAW_HOME

VENVS_ROOT = Path(OPENCLAW_HOME) / "venvs"
# The lock files, in hashing order: the runtime lock, then the dev lock that includes it.
LOCK_NAMES = ("requirements.txt", "requirements-dev.txt")
STAMP = ".rmp-lock.sha256"
INSTALL_TIMEOUT_SEC = 1800

PathLike = Union[str, Path]


def lock_files(root: Path) -> List[Path]:
    """The lock files present under ``root``, in ``LOCK_NAMES`` order."""
    return [root / name for name in LOCK_NAMES if (root / name).is_file()]


def lock_hash(root: Path) -> str:
    """sha256 over each lock file under ``root``: its name and its bytes, each with its length, in order."""
    digest = hashlib.sha256()
    for path in lock_files(root):
        data = path.read_bytes()
        digest.update(f"{path.name}\0{len(data)}\0".encode())
        digest.update(data)
    return digest.hexdigest()


def stamp_of(target: Path) -> Optional[str]:
    """The lock hash ``target`` was built for, or None while it is not a complete venv."""
    try:
        return (target / STAMP).read_text().strip()
    except (FileNotFoundError, NotADirectoryError):
        return None


def install_argv(python: PathLike, root: Path) -> List[str]:
    """pip's install of the lock under ``root``, run from ``root``: the dev lock with its hashes required when there
    is one, else ``requirements.txt`` as it is (the format before WP-09 batch 5, and in a revert of it)."""
    argv = [str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check"]
    if (root / "requirements-dev.txt").is_file():
        return [*argv, "--require-hashes", "-r", "requirements-dev.txt"]
    return [*argv, "-r", "requirements.txt"]


def run(argv: List[str], *, cwd: Optional[Path] = None, timeout: int) -> None:
    """Run ``argv``; a failure raises with the command's last output, which a deploy's log and result show."""
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        output = (done.stderr.strip() or done.stdout.strip())[-600:]
        raise RuntimeError(f"{' '.join(argv[:5])} failed (exit {done.returncode}): {output}")


def build(target: Path, root: Path) -> Path:
    """The venv of the lock files under ``root`` at ``target``. A target stamped for them is returned as it is; any
    other is removed and built again: the venv, the install, ``pip check``, then the stamp, last."""
    wanted = lock_hash(root)
    if stamp_of(target) == wanted:
        return target
    shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    python = target / "bin" / "python"
    run(["python3", "-m", "venv", str(target)], timeout=300)
    run(install_argv(python, root), cwd=root, timeout=INSTALL_TIMEOUT_SEC)
    run([str(python), "-m", "pip", "check"], timeout=300)
    (target / STAMP).write_text(f"{wanted}\n")
    return target


def switch(link: Path, target: PathLike, kind_root: Path, adopt: Optional[str] = None) -> None:
    """Point ``link`` at ``target``, atomically, through a new link ``<link>.tmp`` (a stale one removed first) renamed
    over it; a resumed window runs it again.

    The first switch finds ``link`` a real directory: ``adopt``, the lock hash of the files that directory serves, is
    then required. The directory is stamped with it and moved to ``kind_root/<adopt[:16]>``, where ``build`` finds it
    as the venv of those files, and the link takes its place. A missing link (a kill between that move and the link's
    rename) and a link to another path are pointed at ``target``; a link at ``target`` already is left as it is."""
    target, tmp = str(target), link.with_name(f"{link.name}.tmp")
    if os.path.lexists(tmp):
        os.unlink(tmp)
    if link.is_symlink() and os.readlink(link) == target:
        return
    if link.is_dir() and not link.is_symlink():
        if not adopt:
            raise ValueError(f"{link} is a directory; its lock hash is needed to move it")
        (link / STAMP).write_text(f"{adopt}\n")
        os.symlink(target, tmp)
        kind_root.mkdir(parents=True, exist_ok=True)
        os.rename(link, kind_root / adopt[:16])
    else:
        os.symlink(target, tmp)
    os.replace(tmp, link)


def prune(kind_root: Path, keep: Iterable[PathLike]) -> List[str]:
    """Delete every directory under ``kind_root`` that is not in ``keep``; the names deleted."""
    kept = {Path(path) for path in keep}
    gone = []
    if not kind_root.is_dir():
        return gone
    for entry in sorted(kind_root.iterdir()):
        if entry.is_dir() and not entry.is_symlink() and entry not in kept:
            shutil.rmtree(entry)
            gone.append(entry.name)
    return gone
