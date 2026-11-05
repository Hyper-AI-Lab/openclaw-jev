"""The hashed lock, requirements.txt and requirements-dev.txt, which ops/lock_requirements.py generates from the .in
files, and CI's install of it. Pure: it reads the repo's files."""
import importlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# A pin at the start of a requirement line, with the whitespace PEP 508 allows around its extras and the ==.
PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)")
HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirements(name: str) -> list:
    """The file's requirement lines, continuations joined and stripped; comments and blanks left out."""
    text = (ROOT / name).read_text().replace("\\\n", " ")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def pinned(name: str) -> dict:
    """``{canonical name: version}`` of the file's pins, each read from its whole line; ``-r`` lines are not pins."""
    return {canonical(match.group(1)): match.group(2) for line in requirements(name) if (match := PIN.match(line))}


def test_every_requirement_is_pinned_with_its_hashes():
    for name in ("requirements.txt", "requirements-dev.txt"):
        lines = requirements(name)
        if name == "requirements-dev.txt":
            lines = lines[1:]
        assert lines, name
        for line in lines:
            match = PIN.match(line)
            assert match, (name, line)
            hashes = line[match.end():].split()
            assert hashes and all(HASH.fullmatch(word) for word in hashes), (name, line)


def test_the_dev_lock_includes_the_runtime_lock_and_repeats_none_of_it():
    dev = requirements("requirements-dev.txt")
    assert dev[0] == "-r requirements.txt"
    assert not [line for line in dev[1:] + requirements("requirements.txt") if line.startswith("-")]
    assert not set(pinned("requirements-dev.txt")) & set(pinned("requirements.txt"))


def test_every_in_pin_is_in_its_lock_at_the_same_version():
    runtime = pinned("requirements.txt")
    full = {**runtime, **pinned("requirements-dev.txt")}
    assert pinned("requirements.in") and pinned("requirements.in").items() <= runtime.items()
    assert pinned("requirements-dev.in") and pinned("requirements-dev.in").items() <= full.items()
    assert requirements("requirements-dev.in")[0] == "-r requirements.in"


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
