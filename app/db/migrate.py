"""Schema migrations of rmp_db: Alembic on a frozen live baseline (specs/WP-26.md WP26-05; D-2, D-5, D-6, D-16, D-20).

    python -m app.db.migrate upgrade [--url URL] [--allow-empty] [--skip-backup-check] [--timings] [--json]
    python -m app.db.migrate check [--url URL] [--allow-extension NAME]... [--json]
    python -m app.db.migrate current [--url URL] [--json]
    python -m app.db.migrate revision -m MESSAGE

The database is ``--url``, else ``DATABASE_URL``, which must then name rmp_db; Temporal's databases are refused.
Nothing else is read: no settings, no ``/etc/rmp/rmp.env``, no engine of the application's (``app/db/database.py``).

``upgrade`` prints the database's state (``migrate: state``) and the verdicts a write would meet
(``migrate: preflight:``: the backup root and its newest backup, ``pg_dump`` sessions, the advisory lock's holders,
the backup window), then by state:
- ``empty``, no table: refused unless ``--allow-empty``, which only a new database's build passes (the first install,
  the tests, CI), never the deploy or a restore; then built from the baseline up.
- ``ahead``, at a revision this tree does not have (after a revert): nothing runs, unless the database lacks a table,
  column, unique index, foreign key or CHECK of the tree's ``head.json``, which fails the run.
- ``at_head``: nothing runs.
- ``pre_alembic``, tables and no version: the gate stamps ``0001_baseline`` only when the catalog equals
  ``baseline.json`` but for the ``vector`` extension; then as ``behind``.
- ``behind``: each pending revision in a transaction of its own, printed as it commits (``migrate: committed``).
A database with tables is written only outside 03:00-03:45 UTC, with no ``pg_dump`` session on it, and with a backup
newer than 30 h without problems (6 h when a pending revision is a data revision); every write holds the session
advisory lock 7052310001. A lock timeout or a deadlock is retried three more times. A run that reached the database
reports its drift from ``head.json`` (``migrate: drift:``), and every ``upgrade`` ends with one ``migrate-result:``
JSON line, on success and on failure.

``check`` reads the same, read-only, and says what ``upgrade`` would do; ``current`` prints the revision; ``revision``
writes the next ``NNNN_<slug>.py`` from the template, chained on the head.

Exit codes: 0 done; 1 error; 3 the gate's or ``check``'s differences; 75 not now (the backup window, a ``pg_dump``,
the advisory lock held elsewhere, a data revision without a recent backup), which the deploy waits out.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as clock_time, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from alembic import command as alembic_command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import Script, ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.exc import StatementError
from sqlalchemy.pool import NullPool

from app.db import fingerprint
from app.db.revision_kind import kind_of
from app.production import backup_manifest

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
BASELINE = "0001_baseline"
LOCK_KEY = 7052310001
LOCK_WAIT_SEC = 60
LOCK_POLL_SEC = 2
RETRY_AFTER_SEC = (5, 10, 20)
RETRY_SQLSTATES = ("55P03", "40P01")
BACKUP_MAX_AGE_H = 30
DATA_BACKUP_MAX_AGE_H = 6
BACKUP_WINDOW_UTC = (clock_time(3, 0), clock_time(3, 45))
OK, ERROR, DRIFT, NOT_NOW = 0, 1, 3, 75
COMMANDS = ("upgrade", "check", "current")
APPLICATION_NAME = "rmp-migrate"
WATCH_APPLICATION_NAME = "rmp-migrate-watch"
# The session's bounds on lock queues and statements (D-5): a write-blocking request never stalls every writer of a
# table for long, and an idle transaction never holds its locks.
LOCK_TIMEOUT = "5s"
STATEMENT_TIMEOUT = "60s"
IDLE_IN_TRANSACTION_TIMEOUT = "60s"
WATCH_POLL_SEC = 0.5
# The gate's one accepted difference (D-2), and the drift report's, until WP-26 batch 7 drops the unused extension.
ALLOWED_EXTENSIONS = ("vector",)
# No revision removes a foreign key or CHECK, so an ahead database that lacks one lost it by hand (§10 point 8).
HAND_DROPPED_HINT = ("; if a foreign key or CHECK was dropped by hand, re-add it in the hand-DDL form "
                     "(specs/WP-26.md §2), NOT VALID then VALIDATE, before or with the re-land")
TEMPORAL_DATABASES = ("temporal", "temporal_visibility")
LIVE_DATABASE = "rmp_db"
# alembic_version.version_num is a String(32) (alembic/ddl/impl.py).
REVISION_ID_MAX = 32
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def _utcnow() -> datetime:
    """The wrapper's one clock, for the backup window and the backups' ages."""
    return datetime.now(timezone.utc)


def _print(line: str) -> None:
    print(line, flush=True)


class _Stop(Exception):
    """Ends a run with this exit code and message; ``logged`` when the message is printed already."""

    def __init__(self, code: int, message: str, *, logged: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.logged = logged


@dataclass
class _Context:
    log: Callable[[str], None]
    now: datetime
    root: Path
    location: Path
    allow_empty: bool = False
    skip_backup_check: bool = False
    allow_extensions: Sequence[str] = ()
    timings: bool = False
    result: Dict[str, object] = field(default_factory=lambda: {
        "state": None, "from": None, "to": None, "applied": [], "drift": [], "error": None})


@dataclass
class _Preflight:
    newest: Optional[Path] = None
    age_h: Optional[float] = None
    problems: List[str] = field(default_factory=list)
    unreadable: Optional[str] = None
    dumps: List[int] = field(default_factory=list)
    holders: List[int] = field(default_factory=list)
    in_window: bool = False


def database_url(url: Optional[str], *, from_env: bool) -> URL:
    """The URL to migrate, with the psycopg2 driver. ValueError when there is none, when it is not PostgreSQL's (the
    suite's SQLite included), when it names a Temporal database, or when DATABASE_URL names another than rmp_db."""
    if not url:
        raise ValueError("no database: give --url or set DATABASE_URL")
    parsed = fingerprint.psycopg2_url(url)
    if parsed.database in TEMPORAL_DATABASES:
        raise ValueError(f"{parsed.database} is Temporal's database; only rmp_db is migrated here")
    if from_env and parsed.database != LIVE_DATABASE:
        raise ValueError(f"DATABASE_URL names {parsed.database}, not {LIVE_DATABASE}")
    return parsed


def _engine(url: URL, *, read_only: bool, application_name: str = APPLICATION_NAME,
            autocommit: bool = False) -> Engine:
    options = (f"-c lock_timeout={LOCK_TIMEOUT} -c statement_timeout={STATEMENT_TIMEOUT} "
               f"-c idle_in_transaction_session_timeout={IDLE_IN_TRANSACTION_TIMEOUT}")
    if read_only:
        options += " -c default_transaction_read_only=on"
    extra = {"isolation_level": "AUTOCOMMIT"} if autocommit else {}
    # hide_parameters: no error message carries a statement's values, which a data revision's can be.
    return create_engine(url, poolclass=NullPool, hide_parameters=True,
                         connect_args={"application_name": application_name, "options": options}, **extra)


def alembic_config(location: Path, connection: Optional[Connection] = None) -> Config:
    """Alembic's configuration, in code: no alembic.ini. env.py migrates ``connection``."""
    config = Config()
    config.set_main_option("script_location", str(location))
    config.attributes["connection"] = connection
    return config


def _chain(script: ScriptDirectory) -> List[Script]:
    """The tree's revisions from the baseline to the head; the chain is linear (test_single_head)."""
    return list(reversed(list(script.walk_revisions("base", "heads"))))


def _pending(chain: List[Script], current: Optional[str]) -> List[Script]:
    ids = [revision.revision for revision in chain]
    return chain[ids.index(current) + 1:] if current else list(chain)


def _single_line(value: object) -> str:
    return " ".join(str(value).split())


def _database_error(exc: BaseException) -> Optional[BaseException]:
    if isinstance(exc, StatementError) and exc.orig is not None:
        return exc.orig
    return exc if getattr(exc, "pgcode", None) else None


def _sqlstate(exc: BaseException) -> Optional[str]:
    return getattr(_database_error(exc), "pgcode", None)


def _describe(exc: BaseException) -> str:
    """A failure as the output, deploy.log and Kirill's note carry it: a database error's SQLSTATE and primary message,
    never its statement, parameters or DETAIL line, which can hold a data revision's row values; any other error's
    type and message."""
    error = _database_error(exc)
    if error is not None and getattr(error, "pgcode", None):
        primary = getattr(getattr(error, "diag", None), "message_primary", None)
        return f"{error.pgcode} {_single_line(primary or type(error).__name__)}"
    if error is not None:
        return f"{type(error).__name__}: {_single_line(error)}"
    if isinstance(exc, StatementError):
        return f"{type(exc).__name__}: {_single_line(exc.args[0] if exc.args else '')}"
    return f"{type(exc).__name__}: {_single_line(exc)}"


def _state(conn: Connection, chain: List[Script]) -> Tuple[str, Optional[str]]:
    """``empty``, ``pre_alembic``, ``at_head``, ``behind`` or ``ahead``, with the database's revision; committed."""
    heads = MigrationContext.configure(conn).get_current_heads()
    tables = conn.execute(text(
        "SELECT count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND c.relname <> 'alembic_version'")).scalar_one()
    conn.commit()
    if len(heads) > 1:
        raise _Stop(ERROR, f"rmp_db is at {len(heads)} revisions ({', '.join(heads)}); the chain is linear")
    if not heads:
        return ("pre_alembic" if tables else "empty"), None
    ids = [revision.revision for revision in chain]
    if heads[0] not in ids:
        return "ahead", heads[0]
    return ("at_head" if heads[0] == ids[-1] else "behind"), heads[0]


def _stamp_time(name: str) -> datetime:
    return datetime.strptime(name, STAMP_FORMAT).replace(tzinfo=timezone.utc)


def _preflight(conn: Connection, ctx: _Context) -> _Preflight:
    """What a write would meet, printed on every run and enforced only on the write path (``_guard``); committed. It
    only reports: a backup root that does not read, or a newest backup whose stamp is no real time, is a line here and
    "no usable backup" only for a write."""
    found = _Preflight()
    ctx.log(f"migrate: preflight: backup root {ctx.root}")
    try:
        found.newest = backup_manifest.newest(ctx.root)
        if found.newest is not None:
            found.age_h = (ctx.now - _stamp_time(found.newest.name)).total_seconds() / 3600
            found.problems = backup_manifest.problems(found.newest, hashes=False)
    except OSError as exc:
        found.unreadable = f"{ctx.root} does not read ({exc.strerror or type(exc).__name__})"
    except ValueError as exc:
        found.unreadable = f"{found.newest.name}'s stamp is not a time ({_single_line(exc)})"
    if found.unreadable:
        ctx.log(f"migrate: preflight: newest backup: {found.unreadable}")
    elif found.newest is None:
        ctx.log("migrate: preflight: newest backup: none")
    else:
        verdict = f"problems: {'; '.join(found.problems)}" if found.problems else "no problems"
        ctx.log(f"migrate: preflight: newest backup {found.newest.name}, {found.age_h:.1f} h old, {verdict}")
    found.dumps = list(conn.execute(text(
        "SELECT pid FROM pg_catalog.pg_stat_activity WHERE datname = current_database() "
        "AND application_name = 'pg_dump' ORDER BY pid")).scalars())
    found.holders = list(conn.execute(text(
        "SELECT pid FROM pg_catalog.pg_locks WHERE locktype = 'advisory' AND granted "
        "AND classid = CAST(:high AS oid) AND objid = CAST(:low AS oid) AND objsubid = 1 "
        "AND database = (SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database()) ORDER BY pid"),
        {"high": LOCK_KEY >> 32, "low": LOCK_KEY & 0xFFFFFFFF}).scalars())
    conn.commit()
    ctx.log("migrate: preflight: pg_dump sessions: " + (", ".join(f"pid {pid}" for pid in found.dumps) or "none"))
    ctx.log(f"migrate: preflight: advisory lock {LOCK_KEY}: "
            + (f"held by {', '.join(f'pid {pid}' for pid in found.holders)}" if found.holders else "free"))
    start, end = BACKUP_WINDOW_UTC
    found.in_window = start <= ctx.now.time() < end
    ctx.log(f"migrate: preflight: clock {ctx.now:%Y-%m-%d %H:%M} UTC, {'inside' if found.in_window else 'outside'} "
            f"the backup window {start:%H:%M}-{end:%H:%M} UTC")
    return found


def _guard(found: _Preflight, kinds: Dict[str, str], ctx: _Context) -> None:
    """The production write guards of a database with tables (D-5): raises _Stop when it may not be written now."""
    if found.in_window:
        raise _Stop(NOT_NOW, "not now: inside the backup window 03:00-03:45 UTC")
    if found.dumps:
        raise _Stop(NOT_NOW, f"not now: pg_dump is reading this database (pid {', '.join(map(str, found.dumps))})")
    if ctx.skip_backup_check:
        return
    if found.unreadable:
        raise _Stop(ERROR, f"no usable backup: {found.unreadable}")
    if found.newest is None or found.age_h is None:
        raise _Stop(ERROR, f"no backup under {ctx.root}: a database with tables is written only with a backup newer "
                           f"than {BACKUP_MAX_AGE_H} h")
    if found.problems:
        raise _Stop(ERROR, f"the newest backup {found.newest.name} has problems: {'; '.join(found.problems)}")
    if found.age_h > BACKUP_MAX_AGE_H:
        raise _Stop(ERROR, f"the newest backup {found.newest.name} is {found.age_h:.1f} h old: a database with tables "
                           f"is written only with a backup newer than {BACKUP_MAX_AGE_H} h")
    if "data" in kinds.values() and found.age_h > DATA_BACKUP_MAX_AGE_H:
        raise _Stop(NOT_NOW, f"not now: a data revision needs a backup newer than {DATA_BACKUP_MAX_AGE_H} h "
                             f"(newest: {found.age_h:.1f} h)")


def _kinds(pending: List[Script]) -> Dict[str, str]:
    """Each pending revision's kind, from its file's KIND line; a revision without exactly one stops the run."""
    kinds = {}
    for revision in pending:
        kind = kind_of(Path(revision.path).read_text(encoding="utf-8"))
        if kind is None:
            raise _Stop(ERROR, f'{revision.revision} has no valid KIND line (KIND = "schema" or KIND = "data")')
        kinds[revision.revision] = kind
    return kinds


def _lock(conn: Connection) -> bool:
    """The session advisory lock, tried every LOCK_POLL_SEC for LOCK_WAIT_SEC. The tries run in autocommit, each a
    transaction of its own, so between them the session is idle, never idle in a transaction (whose timeout would
    end it); the session's lock outlives them."""
    conn.execution_options(isolation_level="AUTOCOMMIT")
    try:
        deadline = time.monotonic() + LOCK_WAIT_SEC
        while True:
            taken = conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}).scalar_one()
            conn.commit()
            if taken:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(LOCK_POLL_SEC)
    finally:
        conn.execution_options(isolation_level=conn.default_isolation_level)


def _rollback(conn: Connection) -> None:
    try:
        conn.rollback()
    except Exception:  # A connection the server ended: nothing is left to roll back.
        pass


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _live(conn: Connection) -> dict:
    found = fingerprint.fingerprint(conn)
    conn.commit()
    return found


def _head_and_live(conn: Connection, ctx: _Context) -> Optional[Tuple[dict, dict]]:
    """head.json and the live fingerprint, for the drift report; None, with ``migrate: no drift report:`` printed, when
    either cannot be read. A report that cannot be made never changes the run's exit code."""
    try:
        return _load(ctx.location / "head.json"), _live(conn)
    except Exception as exc:
        _rollback(conn)
        ctx.log(f"migrate: no drift report: {_describe(exc)}")
        return None


def _report_drift(conn: Connection, ctx: _Context, allow: Sequence[str],
                  found: Optional[Tuple[dict, dict]] = None) -> List[str]:
    """The database's differences from head.json (``found``, else read now), printed as ``migrate: drift:`` lines;
    they never change the run's exit code."""
    found = found or _head_and_live(conn, ctx)
    if found is None:
        return []
    head, live = found
    lines = fingerprint.diff(head, live, allow_extensions=allow)
    for line in lines:
        ctx.log(f"migrate: drift: {line}")
    if not lines:
        ctx.log("migrate: no drift from head.json")
    ctx.result["drift"] = lines
    return lines


def _unique_shape(definition: str) -> str:
    """A unique index's definition without its name: its table, method, key columns and predicate."""
    return re.sub(r"^CREATE UNIQUE INDEX \S+ ON ", "CREATE UNIQUE INDEX ON ", definition)


def _lacking(head: dict, live: dict) -> List[str]:
    """What a database ahead of this tree lacks of ``head.json`` and the tree's code needs (D-6): a table, a column, a
    unique index (or the unique constraint it backs) with no valid unique twin under another name, a foreign key or
    CHECK, or one that is only NOT VALID where head.json has it validated."""
    found = []
    tables = {item["name"] for item in live.get("tables", [])}
    found += [f"table {item['name']}" for item in head.get("tables", []) if item["name"] not in tables]
    columns = {(item["table"], item["column"]) for item in live.get("columns", [])}
    found += [f"column {item['table']}.{item['column']}" for item in head.get("columns", [])
              if item["table"] in tables and (item["table"], item["column"]) not in columns]
    indexes = {item["name"]: item for item in live.get("indexes", [])}
    shapes = {_unique_shape(item["definition"]) for item in live.get("indexes", []) if item["unique"] and item["valid"]}
    for item in head.get("indexes", []):
        if not item["unique"] or item["table"] not in tables:
            continue
        present = indexes.get(item["name"])
        if present is not None and present["valid"] and present["unique"]:
            continue
        if _unique_shape(item["definition"]) not in shapes:
            found.append(f"unique index {item['name']}: {item['definition']}")
    constraints = {(item["table"], item["name"]): item for item in live.get("constraints", [])}
    for item in head.get("constraints", []):
        if item["type"] not in ("f", "c") or item["table"] not in tables:
            continue
        kind = "foreign key" if item["type"] == "f" else "check"
        present = constraints.get((item["table"], item["name"]))
        if present is None:
            found.append(f"{kind} {item['table']}.{item['name']}: {item['definition']}")
        elif item["validated"] and not present["validated"]:
            found.append(f"{kind} {item['table']}.{item['name']} is NOT VALID; head.json has it validated")
    return found


def _settled(conn: Connection, state: str, current: Optional[str], ctx: _Context) -> int:
    """A database at the head, or ahead of this tree: nothing runs; an ahead one that lacks what the tree needs
    fails (D-6)."""
    if state != "ahead":
        ctx.log(f"migrate: at head {current}")
        _report_drift(conn, ctx, ALLOWED_EXTENSIONS)
        return OK
    # The ahead rule decides the run, so here head.json and the live catalog must be read.
    head, live = _load(ctx.location / "head.json"), _live(conn)
    missing = _lacking(head, live)
    for line in missing:
        ctx.log(f"migrate: lacking: {line}")
    if missing:
        message = f"rmp_db is at {current}, which this tree does not have, and lacks what this tree needs"
        if any(line.startswith(("foreign key ", "check ")) for line in missing):
            message += HAND_DROPPED_HINT
        raise _Stop(ERROR, message)
    ctx.log(f"migrate: rmp_db is at {current}, which this tree does not have; no revision ran (rule 17)")
    _report_drift(conn, ctx, ALLOWED_EXTENSIONS, (head, live))
    return OK


def _gate(conn: Connection, ctx: _Context) -> None:
    """The catalog against baseline.json before the stamp (D-2): only the vector extension may differ. Every index of
    baseline.json is valid, so the equality also means no invalid index."""
    lines = fingerprint.diff(_load(ctx.location / "baseline.json"), _live(conn), allow_extensions=ALLOWED_EXTENSIONS)
    for line in lines:
        ctx.log(f"migrate: gate: {line}")
    if fingerprint.unallowed(lines):
        raise _Stop(DRIFT, "the gate refused: rmp_db differs from baseline.json as listed; nothing was written")
    ctx.log("migrate: gate passed: rmp_db equals baseline.json" + (", but for the allowed lines" if lines else ""))


class _Watcher:
    """While a revision runs, a second session, read-only and in autocommit, polls for the sessions blocking the
    migration's backend and keeps the last set it saw: once a lock timeout has cancelled the statement, nothing
    blocks it any more. Between polls it holds no transaction and no snapshot, which a CREATE INDEX CONCURRENTLY
    would otherwise wait for."""

    QUERY = text(
        "SELECT a.pid, a.application_name, a.state, a.backend_start, now() - a.xact_start AS transaction_age, "
        "a.wait_event_type, a.wait_event, (SELECT l.relation::regclass::text FROM pg_catalog.pg_locks l "
        "WHERE l.pid = :pid AND NOT l.granted AND l.relation IS NOT NULL LIMIT 1) AS waited_for "
        "FROM pg_catalog.pg_stat_activity a WHERE a.pid = ANY (pg_catalog.pg_blocking_pids(:pid)) ORDER BY a.pid")

    def __init__(self, url: URL, pid: int):
        self._engine = _engine(url, read_only=True, application_name=WATCH_APPLICATION_NAME, autocommit=True)
        self._pid = pid
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.blockers: List[tuple] = []

    def __enter__(self) -> "_Watcher":
        self.blockers = []
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll, name="rmp-migrate-watch", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _poll(self) -> None:
        try:
            with self._engine.connect() as conn:
                while True:
                    rows = conn.execute(self.QUERY, {"pid": self._pid}).all()
                    if rows:
                        self.blockers = [tuple(row) for row in rows]
                    if self._stop.wait(WATCH_POLL_SEC):
                        return
        except Exception:  # The watcher only informs; its own failure never stops a migration.
            return

    def close(self) -> None:
        self._engine.dispose()


def _report_failure(conn: Connection, watcher: _Watcher, ctx: _Context) -> None:
    """Who blocked the revision (never their query text), and every INVALID index in public."""
    for pid, name, state, started, age, wait_type, wait_event, relation in watcher.blockers:
        since = started.isoformat(timespec="seconds") if started is not None else "unknown"
        held = f"{age.total_seconds():.1f} s" if age is not None else "none"
        ctx.log(f"migrate: blocker: pid {pid}, application_name {name!r}, state {state}, backend_start {since}, "
                f"transaction age {held}, wait {wait_type}/{wait_event}; the migration waited for {relation}")
    try:
        invalid = list(conn.execute(text(
            "SELECT c.relname FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND NOT i.indisvalid ORDER BY 1")).scalars())
        conn.commit()
    except Exception as exc:
        _rollback(conn)
        ctx.log(f"migrate: invalid indexes: unknown ({_describe(exc)})")
        return
    ctx.log(f"migrate: invalid indexes: {', '.join(invalid) or 'none'}")


def _apply(conn: Connection, config: Config, revision: str, url: URL, pid: int, ctx: _Context) -> float:
    """One revision in its own transaction, retried on a lock timeout or a deadlock; its seconds once committed."""
    watcher = _Watcher(url, pid)
    retries = 0
    try:
        while True:
            started = time.monotonic()
            try:
                if conn.in_transaction():
                    raise RuntimeError("the wrapper would call Alembic inside a transaction")
                with watcher:
                    alembic_command.upgrade(config, revision)
                conn.commit()
                return time.monotonic() - started
            except Exception as exc:
                _rollback(conn)
                failure = f"{revision} failed: {_describe(exc)}"
                ctx.log(f"migrate: {failure}")
                _report_failure(conn, watcher, ctx)
                if _sqlstate(exc) not in RETRY_SQLSTATES or retries == len(RETRY_AFTER_SEC):
                    raise _Stop(ERROR, failure, logged=True) from exc
                delay = RETRY_AFTER_SEC[retries]
                retries += 1
                ctx.log(f"migrate: retrying {revision} in {delay} s (retry {retries} of {len(RETRY_AFTER_SEC)})")
                time.sleep(delay)
    finally:
        watcher.close()


def _upgrade(conn: Connection, url: URL, ctx: _Context) -> int:
    script = ScriptDirectory(str(ctx.location))
    chain = _chain(script)
    state, current = _state(conn, chain)
    ctx.result.update({"state": state, "from": current, "to": current})
    ctx.log(f"migrate: state {state} {current or 'none'}")
    found = _preflight(conn, ctx)
    if state == "empty" and not ctx.allow_empty:
        raise _Stop(ERROR, "rmp_db has no tables: an incomplete restore or a wrong URL; a new database is built "
                           "with --allow-empty")
    if state in ("ahead", "at_head"):
        return _settled(conn, state, current, ctx)
    kinds = _kinds(_pending(chain, current))
    # An empty database holds nothing a backup could repair or a pg_dump could be reading (D-5).
    if state != "empty":
        _guard(found, kinds, ctx)
    pid = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
    conn.commit()
    if not _lock(conn):
        raise _Stop(NOT_NOW, f"not now: another run holds the advisory lock {LOCK_KEY}")
    try:
        again, current = _state(conn, chain)
        if again != state:
            ctx.result["state"] = again
            ctx.log(f"migrate: state {again} {current or 'none'} (under the lock)")
            if again in ("ahead", "at_head"):
                return _settled(conn, again, current, ctx)
            if again == "empty" and not ctx.allow_empty:
                raise _Stop(ERROR, "rmp_db has no tables: an incomplete restore or a wrong URL; a new database is "
                                   "built with --allow-empty")
            kinds = _kinds(_pending(chain, current))
            if state == "empty" and again != "empty":
                _guard(_preflight(conn, ctx), kinds, ctx)
        config = alembic_config(ctx.location, conn)
        if again == "pre_alembic":
            _gate(conn, ctx)
            if conn.in_transaction():
                raise RuntimeError("the wrapper would call Alembic inside a transaction")
            alembic_command.stamp(config, BASELINE)
            conn.commit()
            current = BASELINE
            ctx.result["to"] = BASELINE
            ctx.log(f"migrate: committed {BASELINE} (stamp)")
        for revision in _pending(chain, current):
            seconds = _apply(conn, config, revision.revision, url, pid, ctx)
            ctx.result["applied"].append([revision.revision, round(seconds, 2)])
            ctx.result["to"] = revision.revision
            ctx.log(f"migrate: committed {revision.revision} ({seconds:.2f} s)")
        ctx.log(f"migrate: at head {ctx.result['to']}")
        _report_drift(conn, ctx, ALLOWED_EXTENSIONS)
        if ctx.timings:
            for revision, seconds in ctx.result["applied"]:
                ctx.log(f"migrate: timing: {revision} {seconds:.2f} s")
        return OK
    finally:
        _rollback(conn)
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
            conn.commit()
        except Exception:  # The session is gone, and its lock with it.
            _rollback(conn)


def _check(conn: Connection, ctx: _Context) -> int:
    """Read-only: what upgrade would do. 3 on the gate's differences, on drift, or on what an ahead database lacks."""
    chain = _chain(ScriptDirectory(str(ctx.location)))
    state, current = _state(conn, chain)
    ctx.result.update({"state": state, "from": current, "to": current})
    ctx.log(f"migrate: state {state} {current or 'none'}")
    _preflight(conn, ctx)
    if state == "at_head":
        drift = _report_drift(conn, ctx, ctx.allow_extensions)
        return DRIFT if fingerprint.unallowed(drift) else OK
    if state == "ahead":
        # D-6's verdict needs head.json and the live catalog, as upgrade's does: their failure ends the run.
        head, live = _load(ctx.location / "head.json"), _live(conn)
        missing = _lacking(head, live)
        for line in missing:
            ctx.log(f"migrate: lacking: {line}")
        drift = _report_drift(conn, ctx, ctx.allow_extensions, (head, live))
        return DRIFT if missing or fingerprint.unallowed(drift) else OK
    pending = _pending(chain, current)
    if state == "pre_alembic":
        lines = fingerprint.diff(_load(ctx.location / "baseline.json"), _live(conn),
                                 allow_extensions=ctx.allow_extensions)
        for line in lines:
            ctx.log(f"migrate: gate: {line}")
        if fingerprint.unallowed(lines):
            ctx.log("migrate: the gate would refuse the stamp")
            return DRIFT
        ctx.log(f"migrate: the gate would stamp {BASELINE}")
        pending = pending[1:]
    for revision in pending:
        kind = kind_of(Path(revision.path).read_text(encoding="utf-8")) or "no valid KIND line"
        ctx.log(f"migrate: pending {revision.revision} ({kind})")
    return OK


def _current(conn: Connection, ctx: _Context) -> int:
    state, current = _state(conn, _chain(ScriptDirectory(str(ctx.location))))
    ctx.result.update({"state": state, "from": current, "to": current})
    ctx.log(f"migrate: current {current or 'none'} ({state})")
    return OK


def run(url: Optional[str], *, command: str = "upgrade", script_location: Path = MIGRATIONS,
        backup_root: Optional[Path] = None, skip_backup_check: bool = False, allow_empty: bool = False,
        url_from_env: bool = False, allow_extensions: Sequence[str] = (), timings: bool = False,
        json_result: bool = False, now: Optional[datetime] = None, log: Callable[[str], None] = _print) -> int:
    """One run of ``command`` on the database at ``url``; its exit code. ``upgrade`` always ends with the
    ``migrate-result:`` line, ``check`` and ``current`` with ``json_result``. ``now`` is ``_utcnow()`` unless given."""
    if command not in COMMANDS:
        raise ValueError(f"unknown command {command!r}")
    ctx = _Context(log=log, now=(now or _utcnow()).astimezone(timezone.utc), location=Path(script_location),
                 root=Path(backup_root) if backup_root is not None else backup_manifest.default_root(),
                 allow_empty=allow_empty, skip_backup_check=skip_backup_check, allow_extensions=allow_extensions,
                 timings=timings)
    code = ERROR
    try:
        try:
            target = database_url(url, from_env=url_from_env)
        except ValueError as exc:
            raise _Stop(ERROR, str(exc)) from exc
        engine = _engine(target, read_only=command != "upgrade")
        try:
            with engine.connect() as conn:
                if command == "upgrade":
                    code = _upgrade(conn, target, ctx)
                elif command == "check":
                    code = _check(conn, ctx)
                else:
                    code = _current(conn, ctx)
        finally:
            engine.dispose()
    except _Stop as stop:
        code = stop.code
        ctx.result["error"] = stop.message
        if not stop.logged:
            log(f"migrate: {stop.message}")
    except Exception as exc:
        code = ERROR
        ctx.result["error"] = _describe(exc)
        log(f"migrate: error: {ctx.result['error']}")
    if command == "upgrade" or json_result:
        log("migrate-result: " + json.dumps(ctx.result))
    return code


def _slug(message: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")


def new_revision(message: str, *, script_location: Path = MIGRATIONS) -> Path:
    """The next revision's file from the template, ``NNNN_<slug>.py`` with the id ``NNNN_<slug>`` (at most 32
    characters), chained on the head; no database is read."""
    location = Path(script_location)
    script = ScriptDirectory(str(location))
    head = script.get_current_head()
    number = int(re.match(r"\d+", head).group(0)) + 1 if head else 1
    revision_id = f"{number:04d}_{_slug(message)}"[:REVISION_ID_MAX].rstrip("_")
    if not _slug(message):
        raise ValueError("the message names no revision: give it words")
    config = alembic_config(location)
    config.set_main_option("file_template", "%%(rev)s")
    alembic_command.revision(config, message=message, rev_id=revision_id)
    return location / "versions" / f"{revision_id}.py"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.db.migrate", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {
        "upgrade": sub.add_parser("upgrade", help="bring the database to this tree's head"),
        "check": sub.add_parser("check", help="read-only: what upgrade would do"),
        "current": sub.add_parser("current", help="read-only: the database's revision"),
    }
    for name, command in commands.items():
        command.add_argument("--url", help="the database (default: DATABASE_URL, which must name rmp_db)")
        command.add_argument("--json", action="store_true", help="end with the migrate-result line"
                             + (" (upgrade always does)" if name == "upgrade" else ""))
    commands["upgrade"].add_argument("--allow-empty", action="store_true",
                                     help="build a database without tables from the baseline up (a new install)")
    commands["upgrade"].add_argument("--skip-backup-check", action="store_true",
                                     help="write without a recent backup (only a scratch copy, the rehearsal)")
    commands["upgrade"].add_argument("--timings", action="store_true", help="print each applied revision's seconds")
    commands["check"].add_argument("--allow-extension", action="append", default=[], metavar="NAME",
                                   help="allow this extension in the gate and the drift (repeatable)")
    revision = sub.add_parser("revision", help="write the next revision from the template")
    revision.add_argument("-m", "--message", required=True, help="what the revision does")
    args = parser.parse_args(argv)
    if args.command == "revision":
        try:
            print(new_revision(args.message))
        except Exception as exc:
            print(f"migrate: error: {_describe(exc)}", file=sys.stderr)
            return ERROR
        return OK
    url = args.url or os.environ.get("DATABASE_URL", "").strip() or None
    return run(url, command=args.command, url_from_env=args.url is None,
               allow_empty=getattr(args, "allow_empty", False),
               skip_backup_check=getattr(args, "skip_backup_check", False),
               allow_extensions=getattr(args, "allow_extension", ()), timings=getattr(args, "timings", False),
               json_result=args.json)


if __name__ == "__main__":
    sys.exit(main())
