"""Postgres for tests: one throwaway server per run, never this host's live cluster (specs/WP-26.md WP26-06, D-11).

A pytest plugin module. Every module with Postgres tests declares ``pytest_plugins = ["tests.postgres_fixture"]`` and
imports nothing from here: pytest registers a plugin once by its name, so a run starts one server, where a fixture
imported into several modules would be defined, and started, once per module.

The server is the one ``RMP_PG_IT_URL`` names (CI's service container on 127.0.0.1:15432), or else a throwaway local
cluster started from ``RMP_PG_BIN`` (default /usr/lib/postgresql/16/bin): initdb with trust authentication, its Unix
socket in its own temp directory under TMPDIR, and no TCP listener. As root (the harness, direct sessions) the server
runs as nobody through setpriv, so every directory on the cluster's path must be traversable by nobody; otherwise it
runs as the current user (aura-coder in a coding job, the runner in CI). Every server command runs from the cluster's
own directory.

With neither server, or a local one that does not start, every Postgres test skips with the reason, unless
RMP_PG_IT_REQUIRED=1 (the harness and CI), which fails it instead: a coding job's suite never fails because its
sandbox cannot start a server, and a run that must test Postgres never passes without it.

Fixtures:
- ``pg_server`` (session): the server. ``database()`` makes another database for the length of a ``with`` block.
- ``pg_database``: an empty database of its own, owned by the unprivileged role ``rmp_it_owner`` (like ``rmp``),
  C.UTF-8, in Berlin time like production.
- ``pg_live_shaped``: the same, cloned from a template database on which ``app/db/migrations/baseline.sql`` ran as
  ``rmp_it_owner`` without a version table: production before its stamp, its tables owned by the role the tests
  connect as.

Every URL a fixture yields points at the server's own socket directory, or at the ``RMP_PG_IT_URL`` server.
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, List, NoReturn, Optional

import psycopg2
import pytest
from sqlalchemy.engine import URL, make_url

from tests import production_guard

REPO = Path(__file__).resolve().parents[1]
BASELINE_SQL = REPO / "app" / "db" / "migrations" / "baseline.sql"
PG_BIN = "/usr/lib/postgresql/16/bin"
# The uid and gid of nobody, which runs a cluster that root starts.
NOBODY = 65534
SETPRIV = ("setpriv", f"--reuid={NOBODY}", f"--regid={NOBODY}", "--clear-groups", "--")
PORT = 55440
ADMIN = "rmp_it_admin"
OWNER = "rmp_it_owner"
START_TIMEOUT_SEC = 30
# A command that outlives pg_ctl's own wait has hung.
COMMAND_TIMEOUT_SEC = 120
LOCALE = "C.UTF-8"
TIMEZONE = "Europe/Berlin"
# The live database's name, which a test server never uses.
LIVE_DATABASE = "rmp_db"
EXTERNAL_HOSTS = ("127.0.0.1", "localhost")
SERVER_OPTIONS = ("-c listen_addresses= -k {base} -c port={port} -c fsync=off -c synchronous_commit=off "
                  "-c full_page_writes=off -c shared_buffers=32MB -c max_connections=40")


class Unavailable(Exception):
    """No server for the Postgres tests, and why: they skip with the reason, or fail when one is required."""


def unavailable(reason: str) -> NoReturn:
    """Skip the test for ``reason``, or fail it when ``RMP_PG_IT_REQUIRED=1``."""
    if os.environ.get("RMP_PG_IT_REQUIRED") == "1":
        pytest.fail(f"RMP_PG_IT_REQUIRED=1 and no Postgres server: {reason}", pytrace=False)
    pytest.skip(f"no Postgres server: {reason}")


def untraversable(path: str, *, stat: Callable[[str], os.stat_result] = os.stat) -> Optional[str]:
    """The first directory above ``path``, from the root down, that nobody cannot traverse: its others' x bit unset
    and not owned by nobody. None when nobody can reach ``path``."""
    for directory in reversed(Path(path).parents):
        st = stat(str(directory))
        if not st.st_mode & 0o001 and st.st_uid != NOBODY:
            return str(directory)
    return None


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


@dataclass(frozen=True)
class Database:
    """A test database: its name, and URLs for its owner (psycopg2 and asyncpg) and for the server's superuser."""
    name: str
    url: str
    async_url: str
    admin_url: str


class Server:
    """A test server: a local cluster in ``base`` (its socket directory), or the external server at ``host``."""

    def __init__(self, *, admin: str, admin_database: str, port: int, base: Optional[str] = None,
                 host: Optional[str] = None, stop: Optional[Callable[[], None]] = None):
        self.admin = admin
        self.admin_database = admin_database
        self.port = port
        self.base = base
        self.host = host
        self._stop = stop
        self._live_template: Optional[str] = None
        self._made: List[str] = []

    def url(self, user: str, database: str, driver: str = "psycopg2") -> str:
        if self.base is not None:
            url = URL.create(f"postgresql+{driver}", username=user, database=database,
                             query={"host": self.base, "port": str(self.port)})
        else:
            url = URL.create(f"postgresql+{driver}", username=user, host=self.host, port=self.port,
                             database=database)
        return url.render_as_string(hide_password=False)

    def owns(self, url: str) -> bool:
        """Whether ``url`` points at this server: its socket directory, or the external server's host and port."""
        parsed = make_url(url)
        if self.base is not None:
            return (parsed.host is None and parsed.query.get("host") == self.base
                    and parsed.query.get("port") == str(self.port))
        return parsed.host == self.host and parsed.port == self.port and "host" not in parsed.query

    def connect(self, database: Optional[str] = None, user: Optional[str] = None):
        """A psycopg2 connection in autocommit, as the superuser unless ``user`` is given."""
        conn = psycopg2.connect(host=self.base or self.host, port=self.port, user=user or self.admin,
                                dbname=database or self.admin_database)
        conn.autocommit = True
        return conn

    def _execute(self, *statements: str, database: Optional[str] = None, user: Optional[str] = None) -> None:
        conn = self.connect(database, user)
        try:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)
        finally:
            conn.close()

    def prepare(self) -> None:
        """The owner role, once: a login role with no superuser, database or role rights, like ``rmp``."""
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (OWNER,))
                if cur.fetchone() is None:
                    cur.execute(f"CREATE ROLE {OWNER} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
        finally:
            conn.close()

    def _create(self, name: str, template: Optional[str]) -> None:
        source = f"TEMPLATE {template}" if template else (
            f"TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE '{LOCALE}' LC_CTYPE '{LOCALE}'")
        self._execute(f"CREATE DATABASE {name} OWNER {OWNER} {source}",
                      f"ALTER DATABASE {name} SET timezone TO '{TIMEZONE}'")
        self._made.append(name)

    def _drop(self, name: str) -> None:
        self._execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        self._made.remove(name)

    def live_template(self) -> str:
        """The template of every live-shaped database: ``baseline.sql`` run as the owner, no version table."""
        if self._live_template is None:
            name = f"rmp_it_live_{secrets.token_hex(6)}"
            self._create(name, None)
            self._execute(BASELINE_SQL.read_text(encoding="utf-8"), database=name, user=OWNER)
            self._live_template = name
        return self._live_template

    @contextmanager
    def database(self, *, live_shaped: bool = False) -> Iterator[Database]:
        """A database of its own for the ``with`` block, empty or live-shaped, dropped after it."""
        name = f"rmp_it_{secrets.token_hex(6)}"
        self._create(name, self.live_template() if live_shaped else None)
        try:
            database = Database(name=name, url=self.url(OWNER, name), async_url=self.url(OWNER, name, "asyncpg"),
                                admin_url=self.url(self.admin, name))
            for url in (database.url, database.async_url, database.admin_url):
                assert self.owns(url), f"{url} is not the test server's"
            yield database
        finally:
            self._drop(name)

    def close(self) -> None:
        """Drop what this run made on an external server; stop and remove a local cluster."""
        try:
            if self._stop is None:
                for name in list(self._made):
                    self._drop(name)
        finally:
            if self._stop is not None:
                self._stop()


def _run(argv: List[str], *, base: str, as_nobody: bool, log: Optional[str] = None) -> None:
    """One server command, from the cluster's directory. Raises Unavailable naming the command, its exit code and the
    last line of its output, or of the server's log."""
    command = [*SETPRIV, *argv] if as_nobody else argv
    try:
        done = subprocess.run(command, cwd=base, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, timeout=COMMAND_TIMEOUT_SEC)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unavailable(f"{os.path.basename(argv[0])} did not run: {exc}") from exc
    if done.returncode != 0:
        detail = done.stdout
        if log is not None and os.path.isfile(log):
            with open(log, encoding="utf-8", errors="replace") as fh:
                detail = fh.read() or detail
        raise Unavailable(f"{os.path.basename(argv[0])} exited {done.returncode}: {_last_line(detail)}")


def start_local(bin_dir: str) -> Server:
    """A throwaway cluster from ``bin_dir`` in a new directory under TMPDIR; on any failure the directory is
    removed and Unavailable names the reason."""
    if not os.path.isfile(os.path.join(bin_dir, "initdb")):
        raise Unavailable(f"RMP_PG_IT_URL is not set and {bin_dir} has no initdb")
    as_nobody = os.geteuid() == 0
    base = tempfile.mkdtemp(prefix="rmp-pg-")
    data, log = os.path.join(base, "data"), os.path.join(base, "log")
    pg_ctl = os.path.join(bin_dir, "pg_ctl")

    def stop() -> None:
        try:
            if os.path.isfile(os.path.join(data, "postmaster.pid")):
                _run([pg_ctl, "-D", data, "stop", "-m", "immediate"], base=base, as_nobody=as_nobody)
        finally:
            shutil.rmtree(base, ignore_errors=True)

    try:
        if as_nobody:
            blocked = untraversable(base)
            if blocked is not None:
                raise Unavailable(f"nobody cannot traverse {blocked}, on the cluster's path {base}")
            os.chown(base, NOBODY, NOBODY)
        _run([os.path.join(bin_dir, "initdb"), "-D", data, "-U", ADMIN, "--auth=trust", "--no-sync",
              "--encoding=UTF8", f"--locale={LOCALE}"], base=base, as_nobody=as_nobody)
        _run([pg_ctl, "-D", data, "-l", log, "-w", "-t", str(START_TIMEOUT_SEC), "start", "-o",
              SERVER_OPTIONS.format(base=base, port=PORT)], base=base, as_nobody=as_nobody, log=log)
    except BaseException:
        try:
            stop()
        except Unavailable:
            pass
        raise
    return Server(admin=ADMIN, admin_database="postgres", port=PORT, base=base, stop=stop)


def external_server(url_text: str) -> Server:
    """The server ``RMP_PG_IT_URL`` names, after asserting it is a throwaway one on this machine's loopback, on no
    port of this host's services, and not the live database."""
    from app.config import DEFAULT_CODING

    url = make_url(url_text)
    port = url.port or 5432
    problems = []
    if url.get_backend_name() != "postgresql":
        problems.append("it is not a postgresql URL")
    if url.host not in EXTERNAL_HOSTS:
        problems.append(f"its host is {url.host!r}, not 127.0.0.1 or localhost")
    if port in production_guard.ports(DEFAULT_CODING["blocked_tcp_ports"]):
        problems.append(f"its port {port} is one of this host's service ports")
    if url.database == LIVE_DATABASE or "host" in url.query:
        problems.append(f"it names {LIVE_DATABASE} or a socket directory")
    if problems:
        pytest.fail(f"RMP_PG_IT_URL is refused: {'; '.join(problems)}", pytrace=False)
    return Server(admin=url.username or "postgres", admin_database=url.database or "postgres", port=port,
                  host=url.host)


def start() -> Server:
    """The run's server: ``RMP_PG_IT_URL``'s, else a local cluster from ``RMP_PG_BIN``. Raises Unavailable."""
    url = os.environ.get("RMP_PG_IT_URL", "").strip()
    return external_server(url) if url else start_local(os.environ.get("RMP_PG_BIN", PG_BIN))


def ready() -> Server:
    """``start()``'s server with the owner role in place. Raises Unavailable, having stopped a local cluster that
    started but does not answer."""
    server = start()
    try:
        server.prepare()
    except psycopg2.Error as exc:
        server.close()
        raise Unavailable(f"the server does not answer: {_last_line(str(exc))}") from exc
    return server


@pytest.fixture(scope="session")
def pg_server() -> Iterator[Server]:
    try:
        server = ready()
    except Unavailable as exc:
        unavailable(str(exc))
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def pg_database(pg_server: Server) -> Iterator[Database]:
    with pg_server.database() as database:
        yield database


@pytest.fixture
def pg_live_shaped(pg_server: Server) -> Iterator[Database]:
    with pg_server.database(live_shaped=True) as database:
        yield database
