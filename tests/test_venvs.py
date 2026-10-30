"""The venv builder and the venv link, on tmp_path: the venv creation and pip are a recording fake."""
import os
import threading
from pathlib import Path

import pytest

from app.coding import venvs


@pytest.fixture
def tools(monkeypatch):
    """``venvs.run`` with the venv creation, the install and ``pip check`` faked: each call is recorded as (step, argv,
    cwd, whether the target carried a stamp then); the creation makes ``bin/python``."""
    calls = []

    def run(argv, *, cwd=None, timeout):
        if argv[:3] == ["python3", "-m", "venv"]:
            step, target = "venv", Path(argv[3])
            (target / "bin").mkdir(parents=True)
            (target / "bin" / "python").write_text("")
        else:
            step, target = ("check" if argv[-1] == "check" else "install"), Path(argv[0]).parents[1]
        calls.append((step, argv, cwd, (target / venvs.STAMP).exists()))

    monkeypatch.setattr(venvs, "run", run)
    return calls


def lock(root: Path, **files) -> Path:
    """``root`` holding the lock files given as ``name=text`` (``requirements_dev`` names requirements-dev.txt)."""
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (root / f"{name.replace('_', '-')}.txt").write_text(text)
    return root


def test_build_stamps_last_and_rebuilds_an_unstamped_target(tmp_path, tools):
    root = lock(tmp_path / "lock", requirements="x==1\n")
    target = tmp_path / "venvs" / "rmp" / "abc"
    # A build killed before its stamp: the half-built venv is removed, not reused.
    (target / "lib").mkdir(parents=True)
    (target / "lib" / "half-installed").write_text("")
    assert venvs.build(target, root) == target
    python = str(target / "bin" / "python")
    assert tools == [
        ("venv", ["python3", "-m", "venv", str(target)], None, False),
        ("install", [python, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", "requirements.txt"],
         root, False),
        ("check", [python, "-m", "pip", "check"], None, False)]
    assert (target / venvs.STAMP).read_text() == f"{venvs.lock_hash(root)}\n"
    assert not (target / "lib" / "half-installed").exists()


def test_build_rebuilds_a_target_that_carries_another_stamp(tmp_path, tools):
    root = lock(tmp_path / "lock", requirements="x==2\n")
    target = tmp_path / "venvs" / "rmp"
    target.mkdir(parents=True)
    (target / venvs.STAMP).write_text(f"{'0' * 64}\n")
    (target / "from-the-old-lock").write_text("")
    venvs.build(target, root)
    assert [step for step, *_ in tools] == ["venv", "install", "check"]
    assert venvs.stamp_of(target) == venvs.lock_hash(root) and not (target / "from-the-old-lock").exists()


def test_build_reuses_a_target_with_the_right_stamp(tmp_path, tools):
    root = lock(tmp_path / "lock", requirements="x==1\n")
    target = tmp_path / "venvs" / "rmp" / "abc"
    target.mkdir(parents=True)
    (target / venvs.STAMP).write_text(f"{venvs.lock_hash(root)}\n")
    (target / "installed").write_text("")
    assert venvs.build(target, root) == target
    assert tools == [] and (target / "installed").exists()


def test_lock_files_take_only_the_present_files(tmp_path):
    root = lock(tmp_path / "a", requirements="x==1\n")
    (root / "requirements-heavy.txt").write_text("y==1\n")
    (root / "requirements.in").write_text("x==1\n")
    assert venvs.lock_files(root) == [root / "requirements.txt"]
    # requirements.txt alone hashes that file alone: other files change nothing.
    assert venvs.lock_hash(root) == venvs.lock_hash(lock(tmp_path / "b", requirements="x==1\n"))
    assert venvs.lock_hash(root) != venvs.lock_hash(lock(tmp_path / "c", requirements="x==2\n"))
    dev = lock(tmp_path / "d", requirements="x==1\n", requirements_dev="-r requirements.txt\n")
    assert venvs.lock_files(dev) == [dev / "requirements.txt", dev / "requirements-dev.txt"]
    assert venvs.lock_hash(dev) != venvs.lock_hash(root)
    # The name counts too: the same bytes under the other name are another lock.
    assert venvs.lock_hash(lock(tmp_path / "e", requirements_dev="x==1\n")) != venvs.lock_hash(root)


def test_install_argv_requires_hashes_for_the_dev_lock(tmp_path):
    dev = lock(tmp_path / "dev", requirements="x==1\n", requirements_dev="-r requirements.txt\npytest==9.0.3\n")
    assert venvs.install_argv("/v/bin/python", dev) == [
        "/v/bin/python", "-m", "pip", "install", "-q", "--disable-pip-version-check", "--require-hashes", "-r",
        "requirements-dev.txt"]
    plain = lock(tmp_path / "plain", requirements="x==1\n")
    assert venvs.install_argv(Path("/v/bin/python"), plain) == [
        "/v/bin/python", "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", "requirements.txt"]


def live_venv(tmp_path: Path) -> Path:
    """A live checkout whose venv is a real directory, as before the first switch."""
    link = tmp_path / "live" / "venv"
    (link / "bin").mkdir(parents=True)
    (link / "bin" / "python").write_text("production's\n")
    return link


def test_switch_adopts_a_real_directory_under_its_lock_hash(tmp_path):
    link, kind_root = live_venv(tmp_path), tmp_path / "venvs" / "rmp"
    target = kind_root / ("b" * 16)
    target.mkdir(parents=True)
    with pytest.raises(ValueError, match="its lock hash is needed"):
        venvs.switch(link, target, kind_root)
    assert link.is_dir() and not link.is_symlink()

    venvs.switch(link, target, kind_root, adopt="a" * 64)
    moved = kind_root / ("a" * 16)
    assert (moved / "bin" / "python").read_text() == "production's\n"
    assert venvs.stamp_of(moved) == "a" * 64
    assert link.is_symlink() and os.readlink(link) == str(target)
    assert sorted(os.listdir(link.parent)) == ["venv"]


def test_switch_swaps_links_atomically(tmp_path):
    kind_root = tmp_path / "venvs" / "rmp"
    first, second = kind_root / "1", kind_root / "2"
    for venv in (first, second):
        venv.mkdir(parents=True)
    link = tmp_path / "live" / "venv"
    link.parent.mkdir()
    venvs.switch(link, first, kind_root)
    missing, done = [], threading.Event()

    def read():
        while not done.is_set():
            try:
                os.readlink(link)
            except FileNotFoundError:
                missing.append(True)

    reader = threading.Thread(target=read)
    reader.start()
    try:
        for n in range(400):
            venvs.switch(link, second if n % 2 == 0 else first, kind_root)
    finally:
        done.set()
        reader.join()
    assert missing == [] and os.readlink(link) == str(first)


def test_switch_is_idempotent_and_repairs_a_missing_link(tmp_path):
    link, kind_root = live_venv(tmp_path), tmp_path / "venvs" / "rmp"
    target = kind_root / ("b" * 16)
    target.mkdir(parents=True)
    venvs.switch(link, target, kind_root, adopt="a" * 64)
    before = os.lstat(link)
    venvs.switch(link, target, kind_root, adopt="a" * 64)
    assert os.lstat(link).st_ino == before.st_ino and os.readlink(link) == str(target)

    # Killed after the directory moved and before the new link took its place: no link, a stale new one.
    link.unlink()
    os.symlink(kind_root / "elsewhere", link.with_name("venv.tmp"))
    venvs.switch(link, target, kind_root)
    assert os.readlink(link) == str(target) and sorted(os.listdir(link.parent)) == ["venv"]

    # A link to another venv, a revert's switch back.
    previous = kind_root / ("a" * 16)
    venvs.switch(link, previous, kind_root)
    assert os.readlink(link) == str(previous)


def test_prune_keeps_what_it_is_told(tmp_path):
    kind_root = tmp_path / "venvs" / "rmp"
    for name in ("target", "previous", "older", "oldest"):
        (kind_root / name / "bin").mkdir(parents=True)
    (kind_root / "note.txt").write_text("")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (kind_root / "linked").symlink_to(elsewhere)
    assert venvs.prune(kind_root, {kind_root / "target", str(kind_root / "previous")}) == ["older", "oldest"]
    assert sorted(os.listdir(kind_root)) == ["linked", "note.txt", "previous", "target"] and elsewhere.is_dir()
    assert venvs.prune(tmp_path / "none", ()) == []


def test_a_failed_step_raises_with_its_output(tmp_path):
    with pytest.raises(RuntimeError, match=r"^sh -c echo .* failed \(exit 3\): no matching distribution$"):
        venvs.run(["sh", "-c", "echo 'no matching distribution' >&2; exit 3"], cwd=tmp_path, timeout=30)
    venvs.run(["sh", "-c", "exit 0"], timeout=30)
