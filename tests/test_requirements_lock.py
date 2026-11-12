"""The hashed lock, requirements.txt and requirements-dev.txt, which ops/lock_requirements.py generates from the .in
files, and CI's install of it. Pure: it reads the repo's files. The .in files are read with the lock script's own
reader, so the two can never disagree about them."""
import importlib
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# A pin in a lock line: its first word, name==version, with no extras and no whitespace. A version begins with a letter
# or digit, so a versionless entry is no pin.
PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][^\s;#\\]*)")
HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirements(name: str) -> list:
    """The file's requirement lines, continuations joined and stripped; comments and blanks left out."""
    text = (ROOT / name).read_text().replace("\\\n", " ")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def pinned(name: str) -> dict:
    """``{canonical name: version}`` of a lock file's pins, each read from its line's first word; its ``-r`` line is no
    pin."""
    return {canonical(match.group(1)): match.group(2)
            for line in requirements(name) if (match := PIN.fullmatch(line.split()[0]))}


def lock_script():
    module_spec = importlib.util.spec_from_file_location("lock_requirements", ROOT / "ops" / "lock_requirements.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def in_pins(name: str) -> dict:
    """``{canonical name: version}`` of a .in file, read by the lock script's ``parse_in_file``, which raises on a file
    outside the .in files' one form: for requirements-dev.in, also unless it holds exactly one include of
    requirements.in and repeats none of its names."""
    return lock_script().parse_in_file(ROOT / name)


def test_every_requirement_is_pinned_with_its_hashes():
    for name in ("requirements.txt", "requirements-dev.txt"):
        lines = requirements(name)
        if name == "requirements-dev.txt":
            lines = lines[1:]
        assert lines, name
        # The script writes each pin as one word, name==version, followed by its hashes and nothing else.
        for line in lines:
            pin, *hashes = line.split()
            assert PIN.fullmatch(pin), (name, line)
            assert hashes and all(HASH.fullmatch(word) for word in hashes), (name, line)


def test_the_dev_lock_includes_the_runtime_lock_and_repeats_none_of_it():
    dev = requirements("requirements-dev.txt")
    assert dev[0] == "-r requirements.txt"
    assert not [line for line in dev[1:] + requirements("requirements.txt") if line.startswith("-")]
    assert not set(pinned("requirements-dev.txt")) & set(pinned("requirements.txt"))


def test_every_in_pin_is_in_its_lock_at_the_same_version():
    runtime = pinned("requirements.txt")
    full = {**runtime, **pinned("requirements-dev.txt")}
    assert in_pins("requirements.in") and in_pins("requirements.in").items() <= runtime.items()
    assert in_pins("requirements-dev.in") and in_pins("requirements-dev.in").items() <= full.items()


def test_starlette_and_pydantic_core_are_pinned_in_the_runtime_lock():
    # WP04-b1-starlette-drift: both are transitive, and an unpinned resolve moved starlette from 0.52.1 to 1.10.0.
    assert {"starlette", "pydantic-core"} <= set(pinned("requirements.txt"))


def test_the_langchain_embedder_dependency_imports():
    # The import of mem0's langchain embedder (mem0/embeddings/langchain.py), which the nvidia and mistral
    # providers use.
    importlib.import_module("langchain.embeddings.base")


def test_ci_installs_the_dev_lock_with_hashes():
    lines = [line.strip() for line in (ROOT / ".github" / "workflows" / "ci.yml").read_text().splitlines()]
    install = lines.index("pip install --require-hashes -r requirements-dev.txt")
    assert lines[install - 1] == "python -m pip install --upgrade pip"
    assert not [line for line in lines if line.startswith("pip install") and "requirements.txt" in line]
