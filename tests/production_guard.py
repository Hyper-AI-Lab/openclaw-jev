"""The test suite's production guard: no test may touch this host's production paths, services or service tools.

The suite runs in CI, in a reviewed coding job (checkout under ``/srv/aura-code/jobs``, ``TMPDIR`` under
``/srv/aura-code/cache/tmp``), in a direct-session clone (under ``/srv/aura-code/direct``) and from ``make go-live``
(the live checkout ``/root/.openclaw/rmp``). So the production roots are denied, the repo root, the interpreter's
prefixes and the temp dir are allowed even inside them, and the live state a repo root holds in the live checkout is
denied even there. The conftest builds its session guard from here before anything else runs.

``install`` puts the guard into the process: an audit hook (PEP 578) checks every path, SQLite database, process and
socket the process opens, and a wrapper checks Temporal's ``Client.connect``. A refused operation raises at once and
is recorded, so a test whose code swallows the raise still fails at teardown.

Limits:
- ``os.stat`` and ``Path.exists()`` are not audited;
- I/O inside C libraries after the audited call is not seen;
- child processes are invisible: only their argv and cwd are checked;
- Temporal's Rust core is invisible apart from ``Client.connect``.
"""
from __future__ import annotations

import errno
import functools
import ipaddress
import os
import re
import shlex
import shutil
import socket
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import (
    Any, Dict, FrozenSet, Iterable, Iterator, List, Mapping, NamedTuple, Optional, Sequence, Tuple, Union,
)

import pytest

PathArg = Union[str, bytes, os.PathLike]

# Any access, read or write: the host's production state and secrets, and the live targets of a deploy
# (deploy.MIRRORS writes plugins, web-stack and Cursor rules; /etc/systemd/system is deploy.UNIT_TARGET, which no
# deploy writes; ops/install_units.py writes deploy.UNIT_TARGET and backs up into /var/backups/rmp-units;
# deploy.CODE_RELOAD_LOCK is the host's code-reload lock).
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
    "/var/backups/rmp-units",
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

# The path arguments of each audit event, by position in CPython 3.12's audit events table.
PATH_EVENTS = {
    "open": (0,), "os.remove": (0,), "os.rename": (0, 1), "os.link": (0, 1), "os.symlink": (0, 1),
    "os.mkdir": (0,), "os.rmdir": (0,), "os.listdir": (0,), "os.scandir": (0,), "os.walk": (0,),
    "os.chmod": (0,), "os.chown": (0,), "os.utime": (0,), "os.truncate": (0,), "os.chdir": (0,),
    "glob.glob": (0,), "glob.glob/2": (0, 2), "pathlib.Path.glob": (0,), "pathlib.Path.rglob": (0,),
    "shutil.copyfile": (0, 1), "shutil.copytree": (0, 1), "shutil.move": (0, 1), "shutil.rmtree": (0,),
    "shutil.chown": (0,), "shutil.make_archive": (0, 2, 3), "shutil.unpack_archive": (0, 1),
}
# Path arguments relative to another argument when that one is given: glob's pattern and make_archive's base_dir are
# relative to their root_dir.
RELATIVE_TO = {"glob.glob/2": {0: 2}, "shutil.make_archive": {3: 2}}
# Where an event carries a dir_fd. A name relative to a directory fd cannot be resolved without a syscall, so such a
# call is skipped. Without a dir_fd the os events pass -1, the glob and shutil events None.
DIR_FD = {
    "os.remove": (1,), "os.rename": (2, 3), "os.link": (2, 3), "os.symlink": (2,), "os.mkdir": (2,),
    "os.rmdir": (1,), "os.chmod": (2,), "os.chown": (3,), "os.utime": (3,), "glob.glob/2": (3,),
    "shutil.rmtree": (1,),
}
PROCESS_EVENTS = frozenset({"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn"})
EVENTS = frozenset({*PATH_EVENTS, *PROCESS_EVENTS, "sqlite3.connect", "socket.connect"})
# Not an audit event: the name the Client.connect wrapper checks and records under.
TEMPORAL_CONNECT = "temporalio.client.Client.connect"

# This host's service tools. A program of these names is refused unless it resolves inside the repo root, where the
# fakes of tests/fakes/coding live.
HOST_TOOLS = frozenset({
    "systemctl", "systemd-run", "journalctl", "nft", "docker", "openclaw", "pkill", "killall", "claude",
})
# Commands that run the command after them, with those of their options that take a separate argument.
WRAPPERS = {
    "nohup": (),
    "exec": ("-a",),
    "command": (),
    "env": ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"),
    "nice": ("-n", "--adjustment"),
    "setsid": (),
    "sudo": ("-u", "--user", "-g", "--group", "-C", "--close-from", "-D", "--chdir", "-h", "--host", "-p",
             "--prompt", "-R", "--chroot", "-r", "--role", "-t", "--type", "-T", "--command-timeout", "-U",
             "--other-user"),
    "timeout": ("-s", "--signal", "-k", "--kill-after"),
}
# Shell reserved words a command follows, as in "if true; then systemctl x; fi"; bash's time takes -p.
RESERVED_WORDS = frozenset({"!", "{", "do", "elif", "else", "if", "then", "time", "until", "while"})
SHELLS = frozenset({"sh", "bash", "dash", "zsh"})
SHELL_PUNCTUATION = ";&|()<>`\n"
# A token made only of these separates two simple commands: ; && || | & subshells, $( ) and backquotes, newlines.
SEPARATORS = frozenset(";&|()`\n")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# Bound now: tests stub shutil.which for the code under test, and the guard must still find the real program.
_which = shutil.which
# RMP_QDRANT_IT=1 runs tests/test_deep_index_integration.py against this host's Qdrant: it lifts these ports only.
QDRANT_PORTS = frozenset({6333, 6334})
# How many of the innermost repo frames a violation keeps.
STACK_FRAMES = 6


class Violation(NamedTuple):
    event: str
    target: str
    stack: Tuple[str, ...]


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


def _decoded(value: Any) -> Optional[str]:
    """A path argument as text; None for an fd, None or anything else that is not a path."""
    if isinstance(value, (str, bytes, os.PathLike)):
        return os.fsdecode(value)
    return None


def ports(entries: Iterable[Union[int, str]]) -> FrozenSet[int]:
    """The ports of a blocked_tcp_ports list, whose ranges are written "low-high"."""
    found = set()
    for entry in entries:
        low, _, high = str(entry).partition("-")
        found.update(range(int(low), int(high or low) + 1))
    return frozenset(found)


def _loopback(host: Any) -> bool:
    """A loopback address, localhost, or the unspecified address, which Linux connects to this host."""
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    if not isinstance(host, str):
        return False
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        name = host.lower().rstrip(".")
        return name in ("", "localhost") or name.endswith(".localhost")
    mapped = getattr(address, "ipv4_mapped", None)
    return any(a is not None and (a.is_loopback or a.is_unspecified) for a in (address, mapped))


def _host_port(target: str) -> Tuple[Optional[str], Optional[int]]:
    """The host and port of a Temporal target such as ``localhost:7233`` or ``http://[::1]:7233``."""
    rest = target.split("://", 1)[-1].split("/", 1)[0]
    host, separator, port = rest.rpartition(":")
    if not separator or not port.isdigit():
        return None, None
    return host.strip("[]"), int(port)


def _program_index(words: Sequence[str]) -> Optional[int]:
    """Where a command's program is: after leading NAME=value words, reserved words and wrappers with their
    options."""
    i = 0
    while i < len(words):
        name = os.path.basename(words[i])
        if _ASSIGNMENT.match(words[i]):
            i += 1
            continue
        if name not in WRAPPERS and name not in RESERVED_WORDS:
            return i
        i += 1
        while i < len(words) and words[i].startswith("-"):
            option = words[i]
            i += 1
            if option == "--":
                break
            if option in WRAPPERS.get(name, ()):
                i += 1
        if name == "timeout":
            i += 1  # the duration
    return None


def _shell_string(arguments: Sequence[str]) -> Optional[str]:
    """A shell's command string: the argument after its first option word with a single dash and a c (-c, -ec)."""
    for i, word in enumerate(arguments[:-1]):
        if word.startswith("-") and not word.startswith("--") and "c" in word:
            return arguments[i + 1]
    return None


def _simple_commands(script: str) -> List[List[str]]:
    """A shell string's simple commands, as words. Raises ValueError when shlex cannot split it.

    Comments are not stripped: shlex would drop the newline that ends a comment, and the next line's command would
    join the commented one.
    """
    lexer = shlex.shlex(script, posix=True, punctuation_chars=SHELL_PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    commands: List[List[str]] = []
    words: List[str] = []
    for token in lexer:
        if token and set(token) <= SEPARATORS:
            if words:
                commands.append(words)
            words = []
        else:
            words.append(token)
    if words:
        commands.append(words)
    return commands


class Guard:
    """Denies a path under a denied root and under no allowed root, and the repo root's live state anywhere.

    The roots are resolved once, here. A checked path is checked literally and resolved, so a symlink out of an
    allowed root into a denied one is denied. ``check`` also refuses this host's service tools and its services on
    loopback. Installed, the guard records what it refused in ``violations``; ``active`` turns it off, because an
    audit hook cannot be removed.
    """

    def __init__(self, denied_roots: Iterable[PathArg], allowed_roots: Iterable[PathArg], repo_root: PathArg):
        self.denied_roots = _forms(denied_roots)
        self.allowed_roots = _forms(allowed_roots)
        self.repo_roots = _forms((repo_root,))
        self.always_denied = _forms(os.path.join(os.fsdecode(repo_root), name) for name in ALWAYS_DENIED)
        self.service_ports: Optional[FrozenSet[int]] = None
        self.violations: List[Violation] = []
        self.active = True
        self._lock = threading.Lock()
        self._local = threading.local()

    def denies(self, path: PathArg) -> bool:
        literal = os.path.abspath(os.fsdecode(path))
        for form in (literal, os.path.realpath(literal)):
            if _under(form, self.always_denied):
                return True
            if _under(form, self.denied_roots) and not _under(form, self.allowed_roots):
                return True
        return False

    def load_service_ports(self) -> FrozenSet[int]:
        """This host's services on loopback: app.config's blocked_tcp_ports, the ports coding units may not reach.

        Read on first use: app.config resolves its paths at import, so it is imported only once the conftest has
        set the test environment.
        """
        if self.service_ports is None:
            from app.config import DEFAULT_CODING

            self.service_ports = ports(DEFAULT_CODING["blocked_tcp_ports"])
        return self.service_ports

    def check(self, event: str, args: Sequence[Any]) -> Optional[str]:
        """What ``event`` with ``args`` would reach in production, or None. Records nothing and raises nothing."""
        if event in PATH_EVENTS:
            return self._paths(event, args)
        if event == "sqlite3.connect":
            database = _decoded(args[0])
            if database is not None:
                database = database.removeprefix("file:").split("?")[0]
                if self.denies(database):
                    return f"production database {database}"
            return None
        if event in PROCESS_EVENTS:
            return self._process(event, args)
        if event == "socket.connect":
            return self._socket(*args)
        if event == TEMPORAL_CONNECT:
            host, port = _host_port(str(args[0]))
            if host is not None and self._service(host, port):
                return f"this host's Temporal at {args[0]}"
        return None

    def _paths(self, event: str, args: Sequence[Any]) -> Optional[str]:
        if any(args[i] not in (None, -1) for i in DIR_FD.get(event, ())):
            return None
        # os.open passes no dir_fd to the event (its mode is None, a builtin open's is a string): a relative name
        # there may be relative to a directory fd, as in shutil.rmtree.
        if event == "open" and args[1] is None and not os.path.isabs(_decoded(args[0]) or "/"):
            return None
        for position in PATH_EVENTS[event]:
            path = _decoded(args[position])
            if path is None:
                continue
            anchor = RELATIVE_TO.get(event, {}).get(position)
            root = None if anchor is None else _decoded(args[anchor])
            if root is not None:
                path = os.path.join(root, path)
            if event == "os.symlink" and position == 0:
                # A link's target is relative to the link's own directory.
                path = os.path.join(os.path.dirname(os.path.abspath(os.fsdecode(args[1]))), path)
            if self.denies(path):
                return f"production path {path}"
        return None

    def _process(self, event: str, args: Sequence[Any]) -> Optional[str]:
        cwd, env = None, None
        if event == "os.system":
            return self._script(_decoded(args[0]) or "", os.pathsep.join(os.get_exec_path()))
        if event == "subprocess.Popen":
            executable, argv, cwd, env = args
        elif event == "os.spawn":
            _, executable, argv, env = args
        else:
            executable, argv, env = args
        argv = [_decoded(word) or "" for word in argv]
        program = _decoded(executable) or (argv[0] if argv else "")
        cwd = _decoded(cwd)
        if cwd is not None and self.denies(cwd):
            return f"production working directory {cwd}"
        # A bare name is looked up on PATH, so only a program with a directory part is a path.
        if os.sep in program and self.denies(program):
            return f"production program {program}"
        for word in argv:
            if word.startswith("/") and self.denies(word):
                return f"production path {word}"
        return self._program([program, *argv[1:]], os.pathsep.join(os.get_exec_path(env)))

    def _program(self, words: Sequence[str], search_path: str) -> Optional[str]:
        """A host tool as the command's program, or what a shell's command string reaches."""
        index = _program_index(words)
        if index is None:
            return None
        program = words[index]
        name = os.path.basename(program)
        if name in HOST_TOOLS:
            found = _which(program, path=search_path)
            if found is not None and not self._in_repo(found):
                return f"host tool {found}"
        if name in SHELLS:
            script = _shell_string(words[index + 1:])
            if script is not None:
                return self._script(script, search_path)
        return None

    def _script(self, script: str, search_path: str) -> Optional[str]:
        """Every simple command of a shell string: its program, and each word that is an absolute path."""
        try:
            commands = _simple_commands(script)
        except ValueError:
            return f"a shell string that cannot be split: {script!r}"
        for words in commands:
            for word in words:
                if word.startswith("/") and self.denies(word):
                    return f"production path {word}"
            reached = self._program(words, search_path)
            if reached is not None:
                return reached
        return None

    def _in_repo(self, path: str) -> bool:
        return _under(os.path.abspath(path), self.repo_roots) and _under(os.path.realpath(path), self.repo_roots)

    def _socket(self, sock: Any, address: Any) -> Optional[str]:
        family = getattr(sock, "family", None)
        if family == socket.AF_UNIX:
            path = _decoded(address)
            if path and not path.startswith("\0") and self.denies(path):
                return f"production socket {path}"
            return None
        if family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple) and len(address) >= 2:
            if self._service(address[0], address[1]):
                return f"this host's service on {address[0]}:{address[1]}"
        return None

    def _service(self, host: Any, port: Any) -> bool:
        if not isinstance(port, int) or port not in self.load_service_ports() or not _loopback(host):
            return False
        return not (port in QDRANT_PORTS and os.environ.get("RMP_QDRANT_IT") == "1")

    def refused(self, event: str, args: Sequence[Any]) -> Optional[str]:
        """Checks an operation and records it if refused; the caller raises. None when it is allowed, the guard is
        off, or this thread is already inside the guard."""
        if not self.active or getattr(self._local, "busy", False):
            return None
        self._local.busy = True
        try:
            target = self.check(event, args)
            if target is not None:
                with self._lock:
                    self.violations.append(Violation(event, target, self._stack()))
            return target
        finally:
            self._local.busy = False

    def hook(self, event: str, args: Tuple[Any, ...]) -> None:
        """The audit hook. Every audited operation of the process comes here, so the event name is filtered first."""
        if event not in EVENTS:
            return
        target = self.refused(event, args)
        if target is not None:
            raise PermissionError(errno.EACCES, f"hermetic tests: {target}")

    def _stack(self) -> Tuple[str, ...]:
        """The innermost repo frames outside this module: where the refused operation came from."""
        frames: List[str] = []
        frame = sys._getframe(1)
        while frame is not None and len(frames) < STACK_FRAMES:
            filename = frame.f_code.co_filename
            if filename != __file__ and _under(filename, self.repo_roots):
                where = os.path.relpath(filename, self.repo_roots[0])
                frames.append(f"{where}:{frame.f_lineno} in {frame.f_code.co_name}")
            frame = frame.f_back
        return tuple(frames)

    def take(self) -> List[Violation]:
        """Removes and returns everything recorded so far."""
        with self._lock:
            taken, self.violations = self.violations, []
        return taken

    @contextmanager
    def expect_violations(self) -> Iterator[List[Violation]]:
        """For the guard's own tests: the list it yields gets what was recorded inside, which leaves the record."""
        earlier = self.take()
        recorded: List[Violation] = []
        try:
            yield recorded
        finally:
            recorded.extend(self.take())
            with self._lock:
                self.violations[:0] = earlier


def report(violations: Sequence[Violation]) -> str:
    """Each violation's event and target, and the repo frames it came from."""
    lines = []
    for violation in violations:
        lines.append(f"{violation.event}: {violation.target}")
        lines.extend(f"    at {frame}" for frame in violation.stack)
    return "\n".join(lines)


def install(guard: Guard) -> None:
    """Puts the guard into this process: an audit hook, and a wrapper on Temporal's Client.connect, which the Rust
    core runs where no audit hook sees it."""
    # app.config is read here, once the conftest has set the environment, not first from inside the hook.
    guard.load_service_ports()
    sys.addaudithook(guard.hook)
    from temporalio.client import Client

    connect = Client.connect.__func__

    @functools.wraps(connect)
    async def guarded_connect(cls, target_host, *args, **kwargs):
        target = guard.refused(TEMPORAL_CONNECT, (target_host,))
        if target is not None:
            raise RuntimeError(f"hermetic tests: {target}")
        return await connect(cls, target_host, *args, **kwargs)

    Client.connect = classmethod(guarded_connect)


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
