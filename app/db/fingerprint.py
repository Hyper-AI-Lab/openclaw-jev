"""The catalog fingerprint of rmp_db's public schema, and the differences between two (specs/WP-26.md WP26-03, D-17).

    python -m app.db.fingerprint (--url URL | --from-file OUT) [--compare FILE] [--allow-extension NAME]...

The fingerprint is what one read-only query of pg_catalog, ``fingerprint.sql``, returns: the tables with their kind
and persistence, the columns with their types, nullability, defaults, identity and generation, every index's
definition and validity, every constraint's definition and validation, the extensions, and every other object in
``public``. It sees what Alembic's ``compare_metadata`` does not: index predicates, methods and INCLUDE columns,
validity, defaults' text and extensions. ``migrations/baseline.json`` is the baseline's fingerprint, which a database
must equal before it is stamped (app/db/migrate.py), and ``migrations/head.json`` is what the head revision builds.

The command reads a database (``--url``, else ``DATABASE_URL``; the session is read-only) or the saved output of the
probe form, ``psql -X -A`` running ``fingerprint.sql`` (``--from-file``, which opens no connection). It prints the
fingerprint as JSON or, with ``--compare``, one line per difference from the file's, and exits 3 when a difference
that is not allowed remains. ``--allow-extension`` allows an extension the file does not have.

It imports nothing of RMP's: no settings, no environment file, no database engine of the application.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

QUERY = Path(__file__).with_name("fingerprint.sql")
OK, ERROR, DIFFERENT = 0, 1, 3
# The fingerprint's sections, each item's key within its section, and the kind a difference line names.
KEYS: Dict[str, Tuple[str, ...]] = {
    "tables": ("name",),
    "columns": ("table", "column"),
    "indexes": ("name",),
    "constraints": ("table", "name"),
    "extensions": ("name",),
    "other_objects": ("kind", "name"),
}
KINDS = {"tables": "table", "columns": "column", "indexes": "index", "constraints": "constraint",
         "extensions": "extension"}
ALLOWED = " (allowed)"
# pg_attribute's attidentity and attgenerated.
IDENTITY = {"a": "ALWAYS", "d": "BY DEFAULT"}
GENERATED = {"s": "STORED"}


def psycopg2_url(url: str) -> URL:
    """``url`` with the psycopg2 driver. ValueError for a URL that is not PostgreSQL's, such as the suite's SQLite."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        raise ValueError(f"{parsed.drivername} is not a postgresql URL")
    return parsed.set(drivername="postgresql+psycopg2")


def read_only_engine(url: str) -> Engine:
    """An engine whose every session is read-only (``default_transaction_read_only=on``)."""
    return create_engine(psycopg2_url(url), poolclass=NullPool,
                         connect_args={"options": "-c default_transaction_read_only=on"})


def fingerprint(conn: Connection) -> dict:
    """The fingerprint of the database ``conn`` is connected to. The query is read-only and the caller commits."""
    return dict(conn.exec_driver_sql(QUERY.read_text(encoding="utf-8")).scalar_one())


def parse_probe_output(text: str) -> dict:
    """The fingerprint in the probe form's output: the one line of ``psql -A``'s that starts with ``{``."""
    lines = [line for line in text.splitlines() if line.startswith("{")]
    if len(lines) != 1:
        raise ValueError(f"the probe output has {len(lines)} lines that start with '{{', not one")
    found = json.loads(lines[0])
    if not isinstance(found, dict):
        raise ValueError("the probe output's fingerprint is not a JSON object")
    return found


def _label(section: str, item: dict) -> Tuple[str, str]:
    if section == "other_objects":
        return str(item.get("kind")), str(item.get("name"))
    if section == "columns":
        return KINDS[section], f"{item.get('table')}.{item.get('column')}"
    if section == "constraints":
        return KINDS[section], f"{item.get('table')}.{item.get('name')}"
    return KINDS[section], str(item.get("name"))


def _definition(section: str, item: dict) -> str:
    if section == "tables":
        return f"relkind {item.get('kind')}, persistence {item.get('persistence')}"
    if section == "columns":
        parts = [str(item.get("type"))]
        if item.get("not_null"):
            parts.append("NOT NULL")
        generated = item.get("generated")
        if generated:
            # A generated column's expression is its default's.
            parts.append(f"GENERATED ALWAYS AS {item.get('default')} {GENERATED.get(generated, generated)}")
        elif item.get("default") is not None:
            parts.append(f"DEFAULT {item['default']}")
        if item.get("identity"):
            parts.append(f"GENERATED {IDENTITY.get(item['identity'], item['identity'])} AS IDENTITY")
        return " ".join(parts)
    if section == "indexes":
        return str(item.get("definition")) + ("" if item.get("valid") else " (INVALID)")
    if section == "constraints":
        return str(item.get("definition")) + ("" if item.get("validated") else " NOT VALID")
    if section == "extensions":
        return f"version {item.get('version')}"
    return ""


def _keyed(section: str, items: object) -> Dict[tuple, dict]:
    return {tuple(str(item.get(key)) for key in KEYS[section]): item
            for item in (items if isinstance(items, list) else []) if isinstance(item, dict)}


def diff(expected: dict, actual: dict, *, allow_extensions: Iterable[str] = ()) -> List[str]:
    """One line per difference of ``actual`` from ``expected``: ``+`` only in ``actual``, ``-`` only in ``expected``,
    ``~`` in both but different; then the kind, the name and the definition. An extension of ``allow_extensions``
    that only ``actual`` has is a difference too, marked `` (allowed)``."""
    allowed = set(allow_extensions)
    lines = []
    for section in KEYS:
        want, have = _keyed(section, expected.get(section)), _keyed(section, actual.get(section))
        for key in sorted(want.keys() | have.keys()):
            old, new = want.get(key), have.get(key)
            if old == new:
                continue
            kind, name = _label(section, new if old is None else old)
            if old is None or new is None:
                item = new if old is None else old
                definition = "" if section == "extensions" else _definition(section, item)
                line = f"{'+' if old is None else '-'} {kind} {name}" + (f": {definition}" if definition else "")
                if section == "extensions" and old is None and name in allowed:
                    line += ALLOWED
            else:
                now, was = _definition(section, new), _definition(section, old)
                if now == was:
                    now, was = json.dumps(new, sort_keys=True), json.dumps(old, sort_keys=True)
                line = f"~ {kind} {name}: {now} (expected: {was})"
            lines.append(line)
    return lines


def unallowed(lines: Iterable[str]) -> List[str]:
    """The difference lines that are not allowed."""
    return [line for line in lines if not line.endswith(ALLOWED)]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.db.fingerprint", description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--url", help="the database to read (default: DATABASE_URL); its session is read-only")
    source.add_argument("--from-file", type=Path, help="the saved output of the probe form; opens no connection")
    parser.add_argument("--compare", type=Path, help="a fingerprint JSON file to compare with, such as head.json")
    parser.add_argument("--allow-extension", action="append", default=[], metavar="NAME",
                        help="allow this extension where the file has none (repeatable)")
    args = parser.parse_args(argv)
    try:
        if args.from_file is not None:
            actual = parse_probe_output(args.from_file.read_text(encoding="utf-8"))
        else:
            url = args.url or os.environ.get("DATABASE_URL", "").strip()
            if not url:
                print("no database: give --url or --from-file, or set DATABASE_URL", file=sys.stderr)
                return ERROR
            engine = read_only_engine(url)
            try:
                with engine.connect() as conn:
                    actual = fingerprint(conn)
            finally:
                engine.dispose()
        expected = None if args.compare is None else json.loads(args.compare.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"fingerprint: {exc}", file=sys.stderr)
        return ERROR
    except DBAPIError as exc:
        # The driver's own message, without the statement SQLAlchemy appends.
        print(f"fingerprint: {type(exc.orig).__name__}: {str(exc.orig).strip()}", file=sys.stderr)
        return ERROR
    if expected is None:
        print(json.dumps(actual, indent=2, sort_keys=True))
        return OK
    lines = diff(expected, actual, allow_extensions=args.allow_extension)
    for line in lines:
        print(line)
    return DIFFERENT if unallowed(lines) else OK


if __name__ == "__main__":
    sys.exit(main())
