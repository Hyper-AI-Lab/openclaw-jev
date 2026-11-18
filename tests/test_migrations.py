"""Schema migrations of rmp_db (specs/WP-26.md batch 1): the frozen live baseline, the catalog fingerprint, the
migration environment and its known drift, and the migrate wrapper.

The Postgres tests run on tests/postgres_fixture.py's throwaway server, never the live cluster. Every test here runs
at tests/backup_fixture.py's NOON with a one-hour-old usable backup, so a write to a database with tables meets the
wrapper's backup window and backup rules the same way whatever the hour the suite runs; a test of those rules sets its
own clock and backups.
"""
import ast
import json
import os
import re
import shutil
import textwrap
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

import pytest
from alembic import command as alembic_command
from alembic.autogenerate import compare_metadata
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.db import fingerprint, migrate
from app.production import backup_manifest
from app.db.revision_kind import kind_of
from tests.backup_fixture import NOON, make_backup

pytest_plugins = ["tests.postgres_fixture"]

REPO = Path(__file__).resolve().parents[1]
FINGERPRINT_SQL = REPO / "app" / "db" / "fingerprint.sql"
MIGRATIONS = REPO / "app" / "db" / "migrations"
HEAD = ScriptDirectory(str(MIGRATIONS)).get_current_head()
# Inside the backup window, 03:00-03:45 UTC.
IN_THE_WINDOW = datetime(2026, 11, 12, 3, 10, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _noon_with_a_usable_backup(monkeypatch, tmp_path):
    monkeypatch.setattr(migrate, "_utcnow", lambda: NOON)
    root = tmp_path / "backups"
    make_backup(root, age_h=1)
    monkeypatch.setenv("RMP_BACKUP_ROOT", str(root))


def _engine(url):
    return create_engine(url, poolclass=NullPool)


def _rows(url, sql, **params):
    engine = _engine(url)
    try:
        with engine.connect() as conn:
            rows = [tuple(row) for row in conn.execute(text(sql), params)]
            conn.commit()
            return rows
    finally:
        engine.dispose()


def _execute(url, *statements):
    engine = _engine(url)
    try:
        with engine.connect() as conn:
            for statement in statements:
                conn.exec_driver_sql(statement)
            conn.commit()
    finally:
        engine.dispose()


def _fingerprint(url):
    engine = _engine(url)
    try:
        with engine.connect() as conn:
            return fingerprint.fingerprint(conn)
    finally:
        engine.dispose()


def _version(url):
    """The database's alembic_version rows; None without the table."""
    if not _rows(url, "SELECT to_regclass('public.alembic_version') IS NOT NULL")[0][0]:
        return None
    return [row[0] for row in _rows(url, "SELECT version_num FROM alembic_version")]


def _upgrade(url, lines=None, **kwargs):
    """The wrapper's upgrade of ``url``, its lines collected in ``lines``; its exit code."""
    return migrate.run(url, command="upgrade", log=(lines if lines is not None else []).append, **kwargs)


def _result(lines):
    assert lines and lines[-1].startswith("migrate-result: "), lines
    return json.loads(lines[-1][len("migrate-result: "):])


def _revision(revision, down_revision, body, kind='KIND = "schema"'):
    """A test-only revision file."""
    return (f'"""A test-only revision."""\nfrom alembic import op\nimport sqlalchemy as sa\n\n'
            f"revision = {revision!r}\ndown_revision = {down_revision!r}\nbranch_labels = None\n"
            f"depends_on = None\n{kind}\n\n\ndef upgrade():\n{textwrap.indent(body, '    ')}\n\n\n"
            f'def downgrade():\n    raise RuntimeError("forward only")\n')


def _tree(tmp_path, name, revisions):
    """A copy of the migration tree under ``tmp_path`` with the test-only ``revisions`` (``{file: text}``) added."""
    location = tmp_path / name
    shutil.copytree(MIGRATIONS, location, ignore=shutil.ignore_patterns("__pycache__"))
    for filename, source in revisions.items():
        (location / "versions" / filename).write_text(source)
    return location


def _write_head_json(pg_server, location):
    """``location``'s head.json: what an empty database upgraded to its head is."""
    with pg_server.database() as database:
        lines = []
        assert _upgrade(database.url, lines, allow_empty=True, script_location=location) == 0, "\n".join(lines)
        (location / "head.json").write_text(json.dumps(_fingerprint(database.url), indent=2, sort_keys=True) + "\n")


def _holding(url, statements, release, ready):
    """In a thread: run ``statements`` in a transaction on ``url``, set ``ready``, and commit when ``release`` is
    set."""
    def hold():
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                for statement in statements:
                    conn.exec_driver_sql(statement)
                ready.set()
                release.wait(60)
                conn.commit()
        finally:
            engine.dispose()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert ready.wait(30), "the holding session never took its lock"
    return thread


# --- fingerprint.sql runs as the superuser in the probe form (D-17): one read-only SELECT that calls only these. ---

KEYWORDS = {"IN", "EXISTS", "ANY", "ALL", "ARRAY", "FILTER", "OVER", "AS", "AND", "OR", "NOT", "ON", "WHERE", "FROM",
            "LATERAL", "CAST", "VALUES"}
ALLOWED_FUNCTIONS = {"pg_get_indexdef", "pg_get_constraintdef", "pg_get_expr", "format_type", "regexp_replace",
                     "jsonb_build_object", "jsonb_build_array", "jsonb_agg", "to_jsonb", "array_agg", "string_agg",
                     "coalesce", "count"}
NEVER = {"pg_read_file", "pg_read_binary_file", "pg_stat_file", "pg_terminate_backend", "pg_cancel_backend",
         "pg_reload_conf", "pg_rotate_logfile", "pg_switch_wal", "pg_promote", "set_config", "pg_sleep", "nextval",
         "setval", "query_to_xml"}
NEVER_PREFIXES = ("pg_ls_", "pg_file", "pg_advisory", "pg_create_", "pg_drop_", "pg_replication", "pg_logical", "lo_",
                  "dblink")
WRITES = {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT", "COPY", "SET", "CALL", "DO",
          "LOCK", "NOTIFY", "LOAD"}
IDENTIFIER = re.compile(r"[^\W\d][\w$]*")
DOLLAR_TAG = re.compile(r"\$([^\W\d][\w]*)?\$")


def _quoted(sql: str, start: int, quote: str, backslash: bool) -> int:
    """The end of the quoted token opening at ``start``: a doubled quote is part of it, and so, with ``backslash``,
    is any character after a backslash."""
    i = start + 1
    while i < len(sql):
        if backslash and sql[i] == "\\":
            i += 2
        elif sql[i] == quote and sql[i + 1:i + 2] == quote:
            i += 2
        elif sql[i] == quote:
            return i + 1
        else:
            i += 1
    raise ValueError(f"unterminated {quote} at {start}")


def lex(sql: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """PostgreSQL's tokens of ``sql`` as ``(kind, text)``, comments and whitespace dropped, and what the file may not
    hold whatever it means: E'' and U& literals, dollar quotes and parameters, backslashes, nested comments.

    Strings are standard ``'…'`` with ``''`` doubled (a backslash in them is a character), ``E'…'`` with backslash
    escapes, ``$tag$…$tag$``; ``"…"`` is an identifier with ``""`` doubled; ``--`` runs to the end of the line and
    ``/* */`` nests."""
    tokens: List[Tuple[str, str]] = []
    refused: List[str] = []
    if "\\" in sql:
        refused.append("a backslash")
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch.isspace():
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = len(sql) if end < 0 else end + 1
        elif sql.startswith("/*", i):
            depth, j = 1, i + 2
            while depth:
                if j >= len(sql):
                    raise ValueError(f"unterminated comment at {i}")
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                    refused.append("a nested comment")
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            i = j
        elif ch in "eE" and sql[i + 1:i + 2] == "'":
            j = _quoted(sql, i + 1, "'", backslash=True)
            tokens.append(("string", sql[i:j]))
            refused.append("an E'' literal")
            i = j
        elif ch in "uU" and sql[i + 1:i + 2] == "&":
            refused.append("a U& literal")
            j = _quoted(sql, i + 2, sql[i + 2], backslash=False) if sql[i + 2:i + 3] in ("'", '"') else i + 2
            tokens.append(("string", sql[i:j]))
            i = j
        elif ch in "bBxXnN" and sql[i + 1:i + 2] == "'":
            j = _quoted(sql, i + 1, "'", backslash=False)
            tokens.append(("string", sql[i:j]))
            i = j
        elif ch == "'":
            j = _quoted(sql, i, "'", backslash=False)
            tokens.append(("string", sql[i:j]))
            i = j
        elif ch == '"':
            j = _quoted(sql, i, '"', backslash=False)
            tokens.append(("quoted", sql[i + 1:j - 1].replace('""', '"')))
            i = j
        elif ch == "$":
            refused.append("a dollar quote or parameter")
            tag = DOLLAR_TAG.match(sql, i)
            if tag:
                end = sql.find(tag.group(0), tag.end())
                if end < 0:
                    raise ValueError(f"unterminated dollar quote at {i}")
                tokens.append(("string", sql[i:end + len(tag.group(0))]))
                i = end + len(tag.group(0))
            else:
                j = i + 1
                while j < len(sql) and sql[j].isdigit():
                    j += 1
                tokens.append(("parameter", sql[i:j]))
                i = j
        elif IDENTIFIER.match(sql, i):
            word = IDENTIFIER.match(sql, i).group(0)
            tokens.append(("identifier", word))
            i += len(word)
        elif ch.isdigit():
            j = i
            while j < len(sql) and (sql[j].isdigit() or sql[j] == "."):
                j += 1
            tokens.append(("number", sql[i:j]))
            i = j
        elif sql.startswith("::", i):
            tokens.append(("operator", "::"))
            i += 2
        else:
            tokens.append(("punctuation" if ch in "(),;[]." else "operator", ch))
            i += 1
    return tokens, refused


def calls(tokens: List[Tuple[str, str]]) -> List[str]:
    """Every identifier, bare or double-quoted, with or without a schema, that the next token, ``(``, calls."""
    return [tokens[k - 1][1] for k in range(1, len(tokens))
            if tokens[k] == ("punctuation", "(") and tokens[k - 1][0] in ("identifier", "quoted")]


def sql_problems(sql: str) -> List[str]:
    """Why ``sql`` is not one read-only SELECT that calls only the allowed functions; empty when it is."""
    try:
        tokens, problems = lex(sql)
    except ValueError as exc:
        return [str(exc)]
    words = [value.upper() for kind, value in tokens if kind == "identifier"]
    if not tokens or tokens[0][0] != "identifier" or tokens[0][1].upper() not in ("SELECT", "WITH"):
        problems.append("it does not start with SELECT or WITH")
    if ("punctuation", ";") in tokens[:-1]:
        problems.append("more than one statement")
    problems += [f"it writes: {word}" for word in sorted(set(words) & WRITES)]
    for k in range(1, len(tokens)):
        # Attribute notation, (expr).func, calls func with expr: no field selection on an expression.
        if tokens[k] == ("punctuation", ".") and (tokens[k - 1][1] in (")", "]") or tokens[k - 1][0] == "string"):
            problems.append("a field selection on an expression")
    for k in range(1, len(tokens)):
        if tokens[k] != ("punctuation", "("):
            continue
        kind, name = tokens[k - 1]
        if kind == "identifier" and name.upper() in KEYWORDS:
            continue
        if kind in ("identifier", "quoted") and name.lower() not in ALLOWED_FUNCTIONS:
            problems.append(f"it calls {name}")
    return problems


def test_the_fingerprint_sql_is_one_read_only_select():
    assert sql_problems(FINGERPRINT_SQL.read_text(encoding="utf-8")) == []
    assert not ALLOWED_FUNCTIONS & NEVER
    assert not [name for name in ALLOWED_FUNCTIONS if name.startswith(NEVER_PREFIXES)]
    assert not {name.upper() for name in ALLOWED_FUNCTIONS} & KEYWORDS


@pytest.mark.parametrize("sql, problem", [
    ("SELECT pg_read_file('/etc/rmp/rmp.env')", "it calls pg_read_file"),
    ("SELECT \"set_config\"('search_path', '', false)", "it calls set_config"),
    ("SELECT pg_catalog.pg_terminate_backend(1)", "it calls pg_terminate_backend"),
    ("SELECT pg_read_file ('x')", "it calls pg_read_file"),
    ("SELECT pg_read_file\n('x')", "it calls pg_read_file"),
    ("SELECT pg_read_file/**/('x')", "it calls pg_read_file"),
    ("SELECT pg_read_file -- a comment\n('x')", "it calls pg_read_file"),
    (r"SELECT E'\'', pg_read_file('x'), ''", "it calls pg_read_file"),
    (r"SELECT '\', pg_read_file('x')", "it calls pg_read_file"),
    (r"SELECT E'x', pg_read_file('x')", "it calls pg_read_file"),
    (r"SELECT E'pg_read_file(1)'", "an E'' literal"),
    ("SELECT U&'d\\0061t\\+000061'", "a U& literal"),
    ("SELECT $$x$$, 1", "a dollar quote or parameter"),
    ("SELECT $1", "a dollar quote or parameter"),
    ("SELECT 1 /* a /* nested */ comment */", "a nested comment"),
    ("SELECT ('/x'::text).pg_read_file", "a field selection on an expression"),
    ("SELECT '/x'.pg_read_file", "a field selection on an expression"),
    ("SELECT 1; SELECT 2", "more than one statement"),
    ("WITH gone AS (DELETE FROM tasks RETURNING id) SELECT 1", "it writes: DELETE"),
    ("SET search_path = ''", "it does not start with SELECT or WITH"),
    ("SELECT 1 FROM tasks FOR UPDATE", "it writes: UPDATE"),
    ("SELECT numeric(1)", "it calls numeric"),
])
def test_a_query_outside_the_allow_list_is_refused(sql, problem):
    assert problem in sql_problems(sql)


@pytest.mark.parametrize("sql, called", [
    (r"SELECT E'\'', pg_read_file('x'), ''", ["pg_read_file"]),
    (r"SELECT E'pg_read_file(1)'", []),
    (r"SELECT '\', pg_read_file('x')", ["pg_read_file"]),
    ("SELECT 'it''s pg_read_file(1)', \"a\"\"b\"(2)", ['a"b']),
    ("SELECT $q$ pg_read_file(1) $q$, count(*)", ["count"]),
    ("SELECT /* pg_read_file(1) /* still a comment */ pg_sleep(1) */ coalesce(1)", ["coalesce"]),
])
def test_the_lexer_follows_postgresqls_quoting(sql, called):
    assert calls(lex(sql)[0]) == called


# --- The fingerprint on Postgres. ---

# Each change, after its setup, gives one difference line, which starts as given; compare_metadata sees none of them.
CHANGES = [
    ("a changed partial predicate", [], [
        "DROP INDEX ux_dm_ingest_pending",
        "CREATE UNIQUE INDEX ux_dm_ingest_pending ON dm_ingest_queue (kind, ref_id) WHERE done_at IS NOT NULL"],
     "~ index ux_dm_ingest_pending: CREATE UNIQUE INDEX ux_dm_ingest_pending ON public.dm_ingest_queue USING btree "
     "(kind, ref_id) WHERE (done_at IS NOT NULL)"),
    ("btree in place of GIN", [], [
        "DROP INDEX ix_task_messages_fts", "CREATE INDEX ix_task_messages_fts ON task_messages (content)"],
     "~ index ix_task_messages_fts: CREATE INDEX ix_task_messages_fts ON public.task_messages USING btree (content)"),
    ("an INCLUDE column", [], [
        "DROP INDEX ix_tasks_status", "CREATE INDEX ix_tasks_status ON tasks (status) INCLUDE (goal)"],
     "~ index ix_tasks_status: CREATE INDEX ix_tasks_status ON public.tasks USING btree (status) INCLUDE (goal)"),
    ("a NOT VALID constraint", [], [
        "ALTER TABLE artifacts DROP CONSTRAINT artifacts_process_run_id_fkey",
        "ALTER TABLE artifacts ADD CONSTRAINT artifacts_process_run_id_fkey FOREIGN KEY (process_run_id) "
        "REFERENCES process_runs (id) NOT VALID"],
     "~ constraint artifacts.artifacts_process_run_id_fkey: FOREIGN KEY (process_run_id) REFERENCES "
     "process_runs(id) NOT VALID"),
    ("an INVALID index", [], [
        "UPDATE pg_index SET indisvalid = false WHERE indexrelid = 'public.ix_tasks_status'::regclass"],
     "~ index ix_tasks_status: CREATE INDEX ix_tasks_status ON public.tasks USING btree (status) (INVALID)"),
    ("an extension", [], ["CREATE EXTENSION pg_trgm"], "+ extension pg_trgm"),
    ("a changed default", [], ["ALTER TABLE tasks ALTER COLUMN task_kind SET DEFAULT 'durable'"],
     "~ column tasks.task_kind: character varying DEFAULT 'durable'::character varying"),
    ("an UNLOGGED table", [], ["ALTER TABLE events SET UNLOGGED"], "~ table events: relkind r, persistence u"),
    ("a view", [], ["CREATE VIEW wp26_view AS SELECT id FROM tasks"], "+ view wp26_view"),
    ("a user trigger",
     ["CREATE FUNCTION wp26_noop() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END'"],
     ["CREATE TRIGGER wp26_trigger BEFORE INSERT ON tasks FOR EACH ROW EXECUTE FUNCTION wp26_noop()"],
     "+ trigger tasks.wp26_trigger"),
    ("a sequence", [], ["CREATE SEQUENCE wp26_sequence"], "+ sequence wp26_sequence"),
    ("an identity column", ["ALTER TABLE tasks ADD COLUMN wp26_n integer NOT NULL"],
     ["ALTER TABLE tasks ALTER COLUMN wp26_n ADD GENERATED ALWAYS AS IDENTITY"],
     "~ column tasks.wp26_n: integer NOT NULL GENERATED ALWAYS AS IDENTITY (expected: integer NOT NULL)"),
    ("a generated column", [],
     ["ALTER TABLE tasks ADD COLUMN wp26_g integer GENERATED ALWAYS AS (priority * 2) STORED"],
     "+ column tasks.wp26_g: integer GENERATED ALWAYS AS (priority * 2) STORED"),
    ("numeric(12,2) in place of numeric(10,2)", ["ALTER TABLE tasks ADD COLUMN wp26_amount numeric(10,2)"],
     ["ALTER TABLE tasks ALTER COLUMN wp26_amount TYPE numeric(12,2)"],
     "~ column tasks.wp26_amount: numeric(12,2) (expected: numeric(10,2))"),
]


def test_the_fingerprint_sees_what_compare_metadata_does_not(pg_live_shaped):
    engine = _engine(pg_live_shaped.admin_url)
    try:
        with engine.connect() as conn:
            for case, setup, change, line in CHANGES:
                # Each case in a transaction of its own, rolled back: the next one starts from the same schema.
                transaction = conn.begin()
                try:
                    for statement in setup:
                        conn.exec_driver_sql(statement)
                    before = fingerprint.fingerprint(conn)
                    for statement in change:
                        conn.exec_driver_sql(statement)
                    lines = fingerprint.diff(before, fingerprint.fingerprint(conn))
                finally:
                    transaction.rollback()
                assert len(lines) == 1 and lines[0].startswith(line), (case, lines)
    finally:
        engine.dispose()


def test_a_validation_changes_only_convalidated(pg_live_shaped):
    engine = _engine(pg_live_shaped.url)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("ALTER TABLE tasks ADD CONSTRAINT tasks_parent_task_id_fkey "
                                 "FOREIGN KEY (parent_task_id) REFERENCES tasks (id) NOT VALID")
            before = fingerprint.fingerprint(conn)
            conn.exec_driver_sql("ALTER TABLE tasks VALIDATE CONSTRAINT tasks_parent_task_id_fkey")
            after = fingerprint.fingerprint(conn)
            conn.commit()
    finally:
        engine.dispose()
    [old] = [c for c in before["constraints"] if c["name"] == "tasks_parent_task_id_fkey"]
    [new] = [c for c in after["constraints"] if c["name"] == "tasks_parent_task_id_fkey"]
    assert (old["validated"], new["validated"]) == (False, True)
    assert old["definition"] == new["definition"] == "FOREIGN KEY (parent_task_id) REFERENCES tasks(id)"
    assert {**old, "validated": True} == new
    before["constraints"] = [new if c is old else c for c in before["constraints"]]
    assert before == after
    assert fingerprint.diff(after, after) == []


def test_the_fingerprint_cli_never_writes(pg_live_shaped, monkeypatch, capsys):
    sessions = []
    real = fingerprint.fingerprint

    def observed(conn):
        sessions.append(conn.exec_driver_sql("SHOW transaction_read_only").scalar_one())
        return real(conn)

    monkeypatch.setattr(fingerprint, "fingerprint", observed)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert fingerprint.main(["--url", pg_live_shaped.url]) == 0
    assert sessions == ["on"]
    printed = json.loads(capsys.readouterr().out)
    assert len(printed["tables"]) == 20 and printed["extensions"] == [{"name": "plpgsql", "version": "1.0"}]


def test_the_probe_output_parses(pg_live_shaped):
    engine = _engine(pg_live_shaped.url)
    try:
        with engine.connect() as conn:
            found = fingerprint.fingerprint(conn)
            # psql -A prints the column as jsonb's own text: one line.
            line = conn.exec_driver_sql(
                f"SELECT probe.fingerprint::text FROM ({FINGERPRINT_SQL.read_text(encoding='utf-8')}) AS probe"
            ).scalar_one()
    finally:
        engine.dispose()
    assert "\n" not in line
    assert fingerprint.parse_probe_output(f"SET\nfingerprint\n{line}\n(1 row)\n") == found
    with pytest.raises(ValueError, match="0 lines"):
        fingerprint.parse_probe_output("SET\nfingerprint\n(0 rows)\n")


def test_from_file_opens_no_connection(tmp_path, monkeypatch, capsys):
    def refused(*args, **kwargs):
        raise AssertionError("--from-file opened a connection")

    monkeypatch.setattr(fingerprint, "create_engine", refused)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    expected = {"tables": [{"name": "tasks", "kind": "r", "persistence": "p"}], "columns": [], "indexes": [],
                "constraints": [], "extensions": [{"name": "plpgsql", "version": "1.0"}], "other_objects": []}
    (tmp_path / "head.json").write_text(json.dumps(expected))
    live = {**expected, "extensions": [{"name": "plpgsql", "version": "1.0"}, {"name": "vector", "version": "0.6.0"}]}
    (tmp_path / "live.out").write_text(f"SET\nfingerprint\n{json.dumps(live)}\n(1 row)\n")
    args = ["--compare", str(tmp_path / "head.json"), "--from-file", str(tmp_path / "live.out")]
    assert fingerprint.main([*args, "--allow-extension", "vector"]) == 0
    assert capsys.readouterr().out == "+ extension vector (allowed)\n"
    assert fingerprint.main(args) == 3
    assert capsys.readouterr().out == "+ extension vector\n"


def test_head_json_is_what_head_builds(pg_database):
    lines = []
    assert _upgrade(pg_database.url, lines, allow_empty=True) == 0, "\n".join(lines)
    found = _fingerprint(pg_database.url)
    if os.environ.get("RMP_WRITE_HEAD_JSON") == "1":
        (MIGRATIONS / "head.json").write_text(json.dumps(found, indent=2, sort_keys=True) + "\n")
        pytest.fail("head.json written; run again without RMP_WRITE_HEAD_JSON")
    expected = json.loads((MIGRATIONS / "head.json").read_text())
    assert found == expected, "\n".join(fingerprint.diff(expected, found))


# --- WP26-02: the baseline, live's schema frozen. ---

def test_the_baseline_file_is_clean():
    lines = (MIGRATIONS / "baseline.sql").read_text(encoding="utf-8").splitlines()
    # pg_dump's TOC comments, such as "-- Name: vector; Type: EXTENSION; Schema: -; Owner: -", are only comments.
    statements = [line for line in lines if not line.startswith("--")]
    assert not [line for line in statements if line.startswith(("SET ", "SELECT pg_catalog.set_config", "\\"))]
    assert not [line for line in statements if re.search(r"OWNER TO|GRANT|REVOKE|EXTENSION|alembic_version", line)]
    assert re.fullmatch(r"-- rmp_db's schema as the nightly backup 20\d{6}T\d{6}Z holds it \(specs/WP-26\.md WP26-02\)",
                        lines[0])
    assert sum(line.startswith("CREATE TABLE public.") for line in statements) == 20


def test_the_baseline_builds_the_live_schema(pg_database):
    engine = _engine(pg_database.url)
    try:
        with engine.connect() as conn:
            alembic_command.upgrade(migrate.alembic_config(MIGRATIONS, conn), migrate.BASELINE)
            conn.commit()
            found = fingerprint.fingerprint(conn)
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()
    assert version == "0001_baseline"
    assert found == json.loads((MIGRATIONS / "baseline.json").read_text()), json.dumps(found, indent=2)
    kinds = [constraint["type"] for constraint in found["constraints"]]
    assert len(found["tables"]) == 20
    assert len(found["indexes"]) == 106 and all(index["valid"] for index in found["indexes"])
    assert (kinds.count("p"), kinds.count("f"), kinds.count("u"), kinds.count("c")) == (20, 15, 2, 0)
    assert len([column for column in found["columns"] if column["default"] is not None]) == 8
    assert [extension["name"] for extension in found["extensions"]] == ["plpgsql"] and found["other_objects"] == []


# --- WP26-04: the migration environment and the drift the survey found. ---

def test_env_imports_only_the_models():
    tree = ast.parse((MIGRATIONS / "env.py").read_text())
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert {name for name in imported if name == "app" or name.startswith("app.")} == {"app.db.models"}


def test_single_head():
    script = ScriptDirectory(str(MIGRATIONS))
    assert len(script.get_heads()) == 1
    assert [revision.revision for revision in script.walk_revisions()][-1] == migrate.BASELINE


def _model_drift(url):
    """compare_metadata between the models and the database at ``url``, through env.py's own configuration, as
    ``(op, table, name)``; the database is not written."""
    engine = _engine(url)
    found = []
    try:
        with engine.connect() as conn:
            config = migrate.alembic_config(MIGRATIONS, conn)
            script = ScriptDirectory.from_config(config)

            def compare(heads, context):
                found.extend(compare_metadata(context, context.opts["target_metadata"]))
                return []

            with EnvironmentContext(config, script, fn=compare, dont_mutate=True):
                script.run_env()
            conn.rollback()
    finally:
        engine.dispose()
    normalised = set()
    for entry in found:
        for change in entry if isinstance(entry, list) else [entry]:
            op = change[0]
            if op.startswith("modify_"):
                normalised.add((op, change[2], change[3]))
            elif op in ("add_index", "remove_index"):
                normalised.add((op, change[1].table.name, change[1].name))
            elif op in ("add_fk", "remove_fk", "add_constraint", "remove_constraint"):
                normalised.add((op, change[1].table.name, change[1].name or ",".join(change[1].column_keys)))
            elif op in ("add_column", "remove_column"):
                normalised.add((op, change[2], change[3].name))
            else:
                normalised.add((op, change[1].name, change[1].name))
    return normalised


# The models against the live baseline (kb/data-model.md D1-D9; specs/WP-26.md D-9). WP-26 batch 6 empties it.
KNOWN_DRIFT = {
    # D1, D2: the parent foreign keys live lacks.
    ("add_fk", "tasks", "parent_task_id"),
    ("add_fk", "process_runs", "parent_process_run_id"),
    # D3: the index live lacks.
    ("add_index", "steps", "ix_steps_idempotency_key"),
    # D4-D6: _MIGRATIONS' twins of model indexes.
    ("remove_index", "side_effect_receipts", "ix_side_effect_idempotency"),
    ("remove_index", "memory_links", "ix_memory_links_source"),
    ("remove_index", "memory_links", "ix_memory_links_target"),
    ("remove_index", "tasks", "ix_tasks_session"),
    # D7: the legacy indexes no model declares.
    ("remove_index", "tasks", "ix_tasks_correlation_id"),
    ("remove_index", "tasks", "ix_tasks_task_type"),
    ("remove_index", "tasks", "ix_tasks_user_request_id"),
    # D8: live's NOT NULL task_id, its named UNIQUE constraint, and its non-unique index where the model's is unique.
    ("modify_nullable", "task_registry_entries", "task_id"),
    ("remove_constraint", "task_registry_entries", "task_registry_entries_task_id_key"),
    ("remove_index", "task_registry_entries", "ix_task_registry_entries_task_id"),
    ("add_index", "task_registry_entries", "ix_task_registry_entries_task_id"),
    # D9: tasks.goal is varchar live, Text in the model.
    ("modify_type", "tasks", "goal"),
}


def test_the_models_differ_from_head_by_the_known_drift_only(pg_database):
    lines = []
    assert _upgrade(pg_database.url, lines, allow_empty=True) == 0, "\n".join(lines)
    found = _model_drift(pg_database.url)
    assert found == KNOWN_DRIFT, (f"not known: {sorted(found - KNOWN_DRIFT)}; "
                                  f"known but gone: {sorted(KNOWN_DRIFT - found)}")


# --- WP26-05: the migrate wrapper. ---

def test_an_empty_database_is_built_to_head(pg_database):
    lines = []
    assert _upgrade(pg_database.url, lines, allow_empty=True) == 0, "\n".join(lines)
    assert lines[0] == "migrate: state empty none"
    assert any(line.startswith(f"migrate: committed {migrate.BASELINE} (") for line in lines)
    result = _result(lines)
    assert (result["state"], result["from"], result["to"], result["error"]) == ("empty", None, HEAD, None)
    assert [revision for revision, _ in result["applied"]] == [migrate.BASELINE]
    assert _version(pg_database.url) == [HEAD]
    assert _fingerprint(pg_database.url) == json.loads((MIGRATIONS / "head.json").read_text())


def test_a_second_upgrade_is_a_no_op(pg_database):
    assert _upgrade(pg_database.url, allow_empty=True) == 0
    before = _fingerprint(pg_database.url)
    # From here every session of the owner is read-only: a write would fail the run.
    _execute(pg_database.admin_url,
             f"ALTER ROLE rmp_it_owner IN DATABASE {pg_database.name} SET default_transaction_read_only = on")
    lines = []
    assert _upgrade(pg_database.url, lines) == 0, "\n".join(lines)
    assert lines[0] == f"migrate: state at_head {HEAD}"
    assert not [line for line in lines if line.startswith("migrate: committed")]
    assert _result(lines)["applied"] == [] and _result(lines)["drift"] == []
    assert _fingerprint(pg_database.url) == before and _version(pg_database.url) == [HEAD]


def test_an_empty_database_is_refused_without_allow_empty(pg_database):
    lines = []
    assert _upgrade(pg_database.url, lines) == 1
    message = ("rmp_db has no tables: an incomplete restore or a wrong URL; a new database is built with "
               "--allow-empty")
    assert f"migrate: {message}" in lines and _result(lines)["error"] == message
    assert _rows(pg_database.url, "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") == [(0,)]
    assert _version(pg_database.url) is None


def _no_engine(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("an engine was created")

    monkeypatch.setattr(migrate, "create_engine", refused)


def test_a_database_url_from_the_environment_must_name_rmp_db(monkeypatch, capsys):
    _no_engine(monkeypatch)
    url = "postgresql://rmp@127.0.0.1:15432/postgres"
    lines = []
    assert migrate.run(url, url_from_env=True, log=lines.append) == 1
    assert "migrate: DATABASE_URL names postgres, not rmp_db" in lines
    assert _result(lines)["error"] == "DATABASE_URL names postgres, not rmp_db"
    monkeypatch.setenv("DATABASE_URL", url)
    assert migrate.main(["current"]) == 1
    assert "DATABASE_URL names postgres, not rmp_db" in capsys.readouterr().out
    # The same URL given with --url is accepted; then the engine is made.
    assert migrate.database_url(url, from_env=False).render_as_string() == \
        "postgresql+psycopg2://rmp@127.0.0.1:15432/postgres"
    assert migrate.main(["current", "--url", url]) == 1
    assert "AssertionError: an engine was created" in capsys.readouterr().out


def test_every_upgrade_prints_its_preflight_verdicts(pg_database, tmp_path):
    assert _upgrade(pg_database.url, allow_empty=True) == 0
    root = tmp_path / "other-backups"
    backup = make_backup(root, age_h=2, now=IN_THE_WINDOW, failures=["qdrant"])
    holder = _engine(pg_database.url).connect()
    dump = create_engine(pg_database.url, poolclass=NullPool, connect_args={"application_name": "pg_dump"}).connect()
    try:
        holder_pid = holder.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
        holder.exec_driver_sql(f"SELECT pg_advisory_lock({migrate.LOCK_KEY})")
        holder.commit()
        dump_pid = dump.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
        dump.commit()
        lines = []
        assert _upgrade(pg_database.url, lines, backup_root=root, now=IN_THE_WINDOW) == 0, "\n".join(lines)
    finally:
        holder.close()
        dump.close()
    assert [line for line in lines if line.startswith("migrate: preflight:")] == [
        f"migrate: preflight: backup root {root}",
        f"migrate: preflight: newest backup {backup.name}, 2.0 h old, problems: failed: qdrant",
        f"migrate: preflight: pg_dump sessions: pid {dump_pid}",
        f"migrate: preflight: advisory lock {migrate.LOCK_KEY}: held by pid {holder_pid}",
        "migrate: preflight: clock 2026-11-12 03:10 UTC, inside the backup window 03:00-03:45 UTC",
    ]


ADD_COLUMN = 'op.add_column("tasks", sa.Column("wp26_added", sa.String()))'


def test_each_revision_is_printed_as_it_commits(pg_database, tmp_path):
    location = _tree(tmp_path, "tree", {
        "0002_first.py": _revision("0002_first", HEAD, ADD_COLUMN),
        "0003_second.py": _revision("0003_second", "0002_first", 'raise RuntimeError("the second revision fails")'),
    })
    lines = []
    assert _upgrade(pg_database.url, lines, allow_empty=True, script_location=location) == 1
    committed = [line.split()[2] for line in lines if line.startswith("migrate: committed")]
    assert committed == [migrate.BASELINE, "0002_first"]
    assert "migrate: 0003_second failed: RuntimeError: the second revision fails" in lines
    assert _version(pg_database.url) == ["0002_first"]


def test_a_pre_alembic_database_is_stamped_after_the_gate(pg_live_shaped):
    lines = []
    assert _upgrade(pg_live_shaped.url, lines) == 0, "\n".join(lines)
    assert lines[0] == "migrate: state pre_alembic none"
    assert "migrate: gate passed: rmp_db equals baseline.json" in lines
    assert f"migrate: committed {migrate.BASELINE} (stamp)" in lines
    assert f"migrate: at head {HEAD}" in lines
    result = _result(lines)
    assert (result["state"], result["from"], result["to"], result["applied"]) == ("pre_alembic", None, HEAD, [])
    assert _version(pg_live_shaped.url) == [HEAD]
    assert _fingerprint(pg_live_shaped.url) == json.loads((MIGRATIONS / "head.json").read_text())


@pytest.mark.parametrize("change, line", [
    ("ALTER TABLE tasks ADD COLUMN wp26_extra integer", "migrate: gate: + column tasks.wp26_extra: integer"),
    ("CREATE INDEX wp26_extra ON tasks (requester)",
     "migrate: gate: + index wp26_extra: CREATE INDEX wp26_extra ON public.tasks USING btree (requester)"),
    ("ALTER TABLE steps DROP CONSTRAINT steps_process_run_id_fkey",
     "migrate: gate: - constraint steps.steps_process_run_id_fkey: FOREIGN KEY (process_run_id) "
     "REFERENCES process_runs(id)"),
])
def test_the_gate_refuses_an_unexpected_difference_and_writes_nothing(pg_live_shaped, change, line):
    _execute(pg_live_shaped.url, change)
    lines = []
    assert _upgrade(pg_live_shaped.url, lines) == 3
    assert line in lines
    assert _result(lines)["error"] == ("the gate refused: rmp_db differs from baseline.json as listed; nothing was "
                                       "written")
    assert _version(pg_live_shaped.url) is None


def test_the_gate_accepts_vector_and_nothing_else():
    baseline = json.loads((MIGRATIONS / "baseline.json").read_text())
    with_vector = {**baseline, "extensions": [*baseline["extensions"], {"name": "vector", "version": "0.6.0"}]}
    lines = fingerprint.diff(baseline, with_vector, allow_extensions=migrate.ALLOWED_EXTENSIONS)
    assert lines == ["+ extension vector (allowed)"] and fingerprint.unallowed(lines) == []
    other = {**with_vector, "extensions": [*with_vector["extensions"], {"name": "pg_trgm", "version": "1.6"}]}
    assert fingerprint.unallowed(fingerprint.diff(baseline, other, allow_extensions=migrate.ALLOWED_EXTENSIONS)) == [
        "+ extension pg_trgm"]
    named = {**baseline, "tables": [*baseline["tables"], {"name": "vector", "kind": "r", "persistence": "p"}]}
    assert fingerprint.unallowed(fingerprint.diff(baseline, named, allow_extensions=migrate.ALLOWED_EXTENSIONS)) == [
        "+ table vector: relkind r, persistence p"]
    vector_gone = {**with_vector, "extensions": baseline["extensions"]}
    assert fingerprint.unallowed(fingerprint.diff(with_vector, vector_gone, allow_extensions=["vector"])) == [
        "- extension vector"]


def _read_only(database):
    _execute(database.admin_url,
             f"ALTER ROLE rmp_it_owner IN DATABASE {database.name} SET default_transaction_read_only = on")


def test_a_database_ahead_of_the_tree_is_skipped(pg_database):
    assert _upgrade(pg_database.url, allow_empty=True) == 0
    _execute(pg_database.url, "UPDATE alembic_version SET version_num = '0099_from_a_later_tree'")
    before = _fingerprint(pg_database.url)
    _read_only(pg_database)
    lines = []
    assert _upgrade(pg_database.url, lines) == 0, "\n".join(lines)
    assert lines[0] == "migrate: state ahead 0099_from_a_later_tree"
    assert ("migrate: rmp_db is at 0099_from_a_later_tree, which this tree does not have; no revision ran "
            "(rule 17)") in lines
    assert _result(lines)["applied"] == [] and _result(lines)["to"] == "0099_from_a_later_tree"
    assert _fingerprint(pg_database.url) == before and _version(pg_database.url) == ["0099_from_a_later_tree"]


def test_a_drift_report_that_cannot_be_made_never_changes_the_exit_code(pg_database, tmp_path, monkeypatch):
    assert _upgrade(pg_database.url, allow_empty=True) == 0
    no_head = _tree(tmp_path, "tree", {})
    (no_head / "head.json").unlink()
    for command in ("upgrade", "check"):
        lines = []
        assert migrate.run(pg_database.url, command=command, script_location=no_head, json_result=True,
                           log=lines.append) == 0, "\n".join(lines)
        assert [line for line in lines if line.startswith("migrate: no drift report: FileNotFoundError: ")]
        assert (_result(lines)["drift"], _result(lines)["error"]) == ([], None)

    def unreadable(conn):
        raise RuntimeError("the catalog does not read")

    readable = fingerprint.fingerprint
    monkeypatch.setattr(fingerprint, "fingerprint", unreadable)
    for command in ("upgrade", "check"):
        lines = []
        assert migrate.run(pg_database.url, command=command, json_result=True, log=lines.append) == 0
        assert "migrate: no drift report: RuntimeError: the catalog does not read" in lines
        assert _result(lines)["error"] is None
    # Ahead, the same reads decide whether the tree may run (D-6), so their failure ends the run, in check too.
    monkeypatch.setattr(fingerprint, "fingerprint", readable)
    _execute(pg_database.url, "UPDATE alembic_version SET version_num = '0099_from_a_later_tree'")
    for command in ("upgrade", "check"):
        lines = []
        assert migrate.run(pg_database.url, command=command, script_location=no_head, json_result=True,
                           log=lines.append) == 1, command
        assert _result(lines)["error"].startswith("FileNotFoundError: "), command


def _at_a_revision_the_tree_lacks(database, tmp_path, body):
    """``database`` built by a tree whose head, 0002_r, ``tmp_path``'s other trees do not have."""
    location = _tree(tmp_path, "tree-r", {"0002_r.py": _revision("0002_r", HEAD, body)})
    lines = []
    assert _upgrade(database.url, lines, allow_empty=True, script_location=location) == 0, "\n".join(lines)


def test_a_database_ahead_on_a_sibling_branch_fails_when_the_head_needs_what_it_lacks(pg_server, pg_database,
                                                                                         tmp_path):
    _at_a_revision_the_tree_lacks(pg_database, tmp_path, 'op.add_column("tasks", sa.Column("wp26_r", sa.String()))')
    sibling = _tree(tmp_path, "tree-s", {"0002_s.py": _revision(
        "0002_s", HEAD, 'op.add_column("tasks", sa.Column("wp26_s", sa.String()))')})
    _write_head_json(pg_server, sibling)
    before = _fingerprint(pg_database.url)
    _read_only(pg_database)
    lines = []
    assert _upgrade(pg_database.url, lines, script_location=sibling) == 1
    assert "migrate: lacking: column tasks.wp26_s" in lines
    assert _result(lines)["error"] == ("rmp_db is at 0002_r, which this tree does not have, and lacks what this tree "
                                       "needs")
    assert _fingerprint(pg_database.url) == before and _version(pg_database.url) == ["0002_r"]


def test_a_database_ahead_with_only_index_and_default_differences_is_skipped(pg_database):
    assert _upgrade(pg_database.url, allow_empty=True) == 0
    _execute(pg_database.url,
             "UPDATE alembic_version SET version_num = '0099_from_a_later_tree'",
             "DROP INDEX ix_tasks_status",
             "ALTER TABLE task_messages ALTER COLUMN role DROP DEFAULT",
             "ALTER TABLE tasks ADD CONSTRAINT wp26_extra_fkey FOREIGN KEY (parent_task_id) REFERENCES tasks (id)",
             # Unique, with its unique twin ix_side_effect_receipts_idempotency_key on the same column.
             "DROP INDEX ix_side_effect_idempotency")
    lines = []
    assert _upgrade(pg_database.url, lines) == 0, "\n".join(lines)
    drift = _result(lines)["drift"]
    assert [line.split(":")[0] for line in drift] == [
        "~ column task_messages.role", "- index ix_side_effect_idempotency", "- index ix_tasks_status",
        "+ constraint tasks.wp26_extra_fkey"]
    assert [f"migrate: drift: {line}" for line in drift] == [line for line in lines if line.startswith(
        "migrate: drift:")]


@pytest.mark.parametrize("sibling, database_change, lacked", [
    ({"0002_s.py": _revision("0002_s", HEAD, 'op.execute("ALTER TABLE tasks ADD CONSTRAINT wp26_priority_check '
                                             'CHECK (priority >= 0) NOT VALID")')},
     None, "migrate: lacking: check tasks.wp26_priority_check: CHECK ((priority >= 0))"),
    ({"0002_s.py": _revision("0002_s", HEAD, 'op.execute("ALTER TABLE tasks ADD CONSTRAINT wp26_parent_fkey '
                                             'FOREIGN KEY (parent_task_id) REFERENCES tasks (id) NOT VALID")'),
      "0003_s.py": _revision("0003_s", "0002_s",
                             'op.execute("ALTER TABLE tasks VALIDATE CONSTRAINT wp26_parent_fkey")')},
     "ALTER TABLE tasks ADD CONSTRAINT wp26_parent_fkey FOREIGN KEY (parent_task_id) REFERENCES tasks (id) NOT VALID",
     "migrate: lacking: foreign key tasks.wp26_parent_fkey is NOT VALID; head.json has it validated"),
], ids=["check", "foreign key"])
def test_a_database_ahead_that_lacks_a_foreign_key_or_check_of_the_head_fails(pg_server, pg_database, tmp_path,
                                                                              sibling, database_change, lacked):
    _at_a_revision_the_tree_lacks(pg_database, tmp_path, 'op.add_column("tasks", sa.Column("wp26_r", sa.String()))')
    if database_change:
        _execute(pg_database.url, database_change)
    location = _tree(tmp_path, "tree-s", sibling)
    _write_head_json(pg_server, location)
    before = _fingerprint(pg_database.url)
    lines = []
    assert _upgrade(pg_database.url, lines, script_location=location) == 1
    assert lacked in lines
    assert _result(lines)["error"] == (
        "rmp_db is at 0002_r, which this tree does not have, and lacks what this tree needs; if a foreign key or CHECK "
        "was dropped by hand, re-add it in the hand-DDL form (specs/WP-26.md §2), NOT VALID then VALIDATE, before or "
        "with the re-land")
    assert _fingerprint(pg_database.url) == before and _version(pg_database.url) == ["0002_r"]


def test_a_lock_timeout_is_retried(pg_database, tmp_path, monkeypatch):
    assert _upgrade(pg_database.url, allow_empty=True) == 0
    location = _tree(tmp_path, "tree", {"0002_add.py": _revision("0002_add", HEAD, ADD_COLUMN)})
    monkeypatch.setattr(migrate, "LOCK_TIMEOUT", "2s")
    release, ready = threading.Event(), threading.Event()
    holder = _holding(pg_database.admin_url, ["LOCK TABLE tasks IN ACCESS SHARE MODE"], release, ready)
    timer = threading.Timer(7, release.set)
    timer.start()
    lines = []
    try:
        assert _upgrade(pg_database.url, lines, script_location=location) == 0, "\n".join(lines)
    finally:
        release.set()
        timer.cancel()
        holder.join(30)
    assert "migrate: 0002_add failed: 55P03 canceling statement due to lock timeout" in lines
    assert "migrate: retrying 0002_add in 5 s (retry 1 of 3)" in lines
    assert [revision for revision, _ in _result(lines)["applied"]] == ["0002_add"]
    assert _version(pg_database.url) == ["0002_add"]


def test_a_lock_held_past_every_retry_fails_and_keeps_the_parent(pg_database, tmp_path, monkeypatch):
    assert _upgrade(pg_database.url, allow_empty=True) == 0
    location = _tree(tmp_path, "tree", {"0002_add.py": _revision("0002_add", HEAD, ADD_COLUMN)})
    monkeypatch.setattr(migrate, "LOCK_TIMEOUT", "1s")
    monkeypatch.setattr(migrate, "RETRY_AFTER_SEC", (0.2, 0.2, 0.2))
    monkeypatch.setattr(migrate, "WATCH_POLL_SEC", 0.1)
    release, ready = threading.Event(), threading.Event()
    holder = _holding(pg_database.admin_url, ["LOCK TABLE tasks IN ACCESS SHARE MODE"], release, ready)
    lines = []
    try:
        holder_pid = _rows(pg_database.admin_url, "SELECT pid FROM pg_locks WHERE relation = 'tasks'::regclass "
                                                  "AND mode = 'AccessShareLock' AND granted "
                                                  "AND pid <> pg_backend_pid()")[0][0]
        assert _upgrade(pg_database.url, lines, script_location=location) == 1
    finally:
        release.set()
        holder.join(30)
    assert len([line for line in lines if line == "migrate: 0002_add failed: 55P03 canceling statement due to lock "
                                                  "timeout"]) == 4
    blockers = [line for line in lines if line.startswith("migrate: blocker:")]
    assert blockers and all(line.startswith(f"migrate: blocker: pid {holder_pid}, ") and line.endswith(
        "the migration waited for tasks") for line in blockers)
    assert "migrate: invalid indexes: none" in lines
    assert _result(lines)["error"] == "0002_add failed: 55P03 canceling statement due to lock timeout"
    assert _version(pg_database.url) == [HEAD]
    assert _rows(pg_database.url, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'tasks' "
                                  "AND column_name = 'wp26_added'") == [(0,)]


def test_a_deadlock_is_retried(pg_database, tmp_path, monkeypatch):
    assert _upgrade(pg_database.url, allow_empty=True) == 0
    location = _tree(tmp_path, "tree", {"0002_two.py": _revision("0002_two", HEAD, (
        'op.execute("LOCK TABLE tasks IN ACCESS EXCLUSIVE MODE")\n'
        'op.execute("LOCK TABLE process_runs IN ACCESS EXCLUSIVE MODE")'))})
    monkeypatch.setattr(migrate, "RETRY_AFTER_SEC", (0.5, 0.5, 0.5))
    # The migration's backend checks for the cycle 3 s into its wait; the other side, 10 s into its own.
    _execute(pg_database.admin_url, f"ALTER DATABASE {pg_database.name} SET deadlock_timeout = '3s'")
    release, ready = threading.Event(), threading.Event()
    failures = []

    def other_side():
        engine = _engine(pg_database.admin_url)
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("SET deadlock_timeout = '10s'")
                conn.exec_driver_sql("LOCK TABLE process_runs IN ACCESS EXCLUSIVE MODE")
                ready.set()
                deadline = time.monotonic() + 30
                while not _rows(pg_database.admin_url, (
                        "SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid WHERE NOT l.granted "
                        "AND a.application_name = 'rmp-migrate' AND l.relation = 'process_runs'::regclass")):
                    assert time.monotonic() < deadline, "the migration never waited for process_runs"
                    time.sleep(0.02)
                conn.exec_driver_sql("LOCK TABLE tasks IN ACCESS EXCLUSIVE MODE")
                conn.commit()
        except Exception as exc:
            failures.append(exc)
            ready.set()
        finally:
            engine.dispose()

    thread = threading.Thread(target=other_side, daemon=True)
    thread.start()
    assert ready.wait(30)
    lines = []
    assert _upgrade(pg_database.url, lines, script_location=location) == 0, "\n".join(lines)
    thread.join(30)
    assert failures == []
    assert "migrate: 0002_two failed: 40P01 deadlock detected" in lines
    assert "migrate: retrying 0002_two in 0.5 s (retry 1 of 3)" in lines
    assert _version(pg_database.url) == ["0002_two"]


def test_the_advisory_lock_serialises_two_runners(pg_database, monkeypatch):
    monkeypatch.setattr(migrate, "LOCK_WAIT_SEC", 1)
    monkeypatch.setattr(migrate, "LOCK_POLL_SEC", 0.2)
    holder = _engine(pg_database.url).connect()
    try:
        holder.exec_driver_sql(f"SELECT pg_advisory_lock({migrate.LOCK_KEY})")
        holder.commit()
        lines = []
        started = time.monotonic()
        assert _upgrade(pg_database.url, lines, allow_empty=True) == 75
        assert time.monotonic() - started < 10
    finally:
        holder.close()
    assert _result(lines)["error"] == f"not now: another run holds the advisory lock {migrate.LOCK_KEY}"
    assert _version(pg_database.url) is None


def test_the_wrapper_never_calls_alembic_inside_a_transaction(pg_live_shaped, tmp_path, monkeypatch):
    location = _tree(tmp_path, "tree", {"0002_add.py": _revision("0002_add", HEAD, ADD_COLUMN)})
    calls = []
    for name in ("stamp", "upgrade"):
        real = getattr(alembic_command, name)

        def spy(config, revision, *args, _real=real, _name=name, **kwargs):
            calls.append((_name, revision, config.attributes["connection"].in_transaction()))
            return _real(config, revision, *args, **kwargs)

        monkeypatch.setattr(alembic_command, name, spy)
    monkeypatch.setattr(migrate, "LOCK_WAIT_SEC", 30)
    monkeypatch.setattr(migrate, "LOCK_POLL_SEC", 0.2)
    holder = _engine(pg_live_shaped.admin_url).connect()
    holder.exec_driver_sql(f"SELECT pg_advisory_lock({migrate.LOCK_KEY})")
    holder.commit()
    states, lines = [], []
    runner = threading.Thread(target=lambda: lines.append(_upgrade(pg_live_shaped.url, lines,
                                                                   script_location=location)))
    runner.start()
    try:
        # The lock wait follows the preflight's last line; sampled from then on, each try is its own transaction.
        deadline = time.monotonic() + 30
        while not [line for line in lines if str(line).startswith("migrate: preflight: clock")]:
            assert time.monotonic() < deadline, lines
            time.sleep(0.02)
        time.sleep(0.3)
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            states += [row[0] for row in _rows(pg_live_shaped.admin_url, (
                "SELECT state FROM pg_stat_activity WHERE application_name = 'rmp-migrate' AND datname = :name"),
                name=pg_live_shaped.name)]
            time.sleep(0.05)
    finally:
        holder.exec_driver_sql(f"SELECT pg_advisory_unlock({migrate.LOCK_KEY})")
        holder.commit()
        holder.close()
        runner.join(60)
    assert lines[-1] == 0, "\n".join(lines)
    assert calls == [("stamp", migrate.BASELINE, False), ("upgrade", "0002_add", False)]
    assert states and set(states) <= {"idle", "active"}, states


def test_the_watcher_holds_no_transaction_between_polls(pg_database, tmp_path, monkeypatch):
    assert _upgrade(pg_database.url, allow_empty=True) == 0
    location = _tree(tmp_path, "tree", {"0002_add.py": _revision("0002_add", HEAD, ADD_COLUMN)})
    monkeypatch.setattr(migrate, "LOCK_TIMEOUT", "20s")
    monkeypatch.setattr(migrate, "WATCH_POLL_SEC", 0.2)
    release, ready = threading.Event(), threading.Event()
    holder = _holding(pg_database.admin_url, ["LOCK TABLE tasks IN ACCESS SHARE MODE"], release, ready)
    samples, lines = [], []
    runner = threading.Thread(target=lambda: lines.append(_upgrade(pg_database.url, lines,
                                                                   script_location=location)))
    runner.start()
    try:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            samples += _rows(pg_database.admin_url, (
                "SELECT state, backend_xmin::text FROM pg_stat_activity WHERE application_name = 'rmp-migrate-watch'"))
            time.sleep(0.03)
    finally:
        release.set()
        holder.join(30)
        runner.join(60)
    assert lines[-1] == 0, "\n".join(lines)
    assert samples and {state for state, _ in samples} <= {"idle", "active"}, samples
    assert [xmin for state, xmin in samples if state == "idle"] == [None] * len(
        [state for state, _ in samples if state == "idle"]) and ("idle", None) in samples


def test_the_backup_window_and_a_running_pg_dump_mean_not_now(pg_live_shaped, tmp_path, monkeypatch):
    location = _tree(tmp_path, "tree", {"0002_add.py": _revision("0002_add", HEAD, ADD_COLUMN)})
    # The wrapper's own clock, without now=: the pin every test of this module relies on.
    monkeypatch.setattr(migrate, "_utcnow", lambda: IN_THE_WINDOW)
    lines = []
    assert _upgrade(pg_live_shaped.url, lines, script_location=location) == 75
    assert _result(lines)["error"] == "not now: inside the backup window 03:00-03:45 UTC"
    monkeypatch.setattr(migrate, "_utcnow", lambda: NOON)
    dump = create_engine(pg_live_shaped.url, poolclass=NullPool,
                         connect_args={"application_name": "pg_dump"}).connect()
    try:
        pid = dump.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
        dump.commit()
        lines = []
        assert _upgrade(pg_live_shaped.url, lines, script_location=location) == 75
    finally:
        dump.close()
    assert _result(lines)["error"] == f"not now: pg_dump is reading this database (pid {pid})"
    assert _version(pg_live_shaped.url) is None


def test_writing_to_a_database_with_tables_needs_a_recent_verified_backup(pg_live_shaped, pg_database, tmp_path):
    none, old, failed, good = (tmp_path / name for name in ("none", "old", "failed", "good"))
    none.mkdir()
    stale = make_backup(old, age_h=31)
    broken = make_backup(failed, age_h=1, failures=["postgres"])
    make_backup(good, age_h=1)
    for root, error in [
        (none, f"no backup under {none}: a database with tables is written only with a backup newer than 30 h"),
        (old, f"the newest backup {stale.name} is 31.0 h old: a database with tables is written only with a backup "
              "newer than 30 h"),
        (failed, f"the newest backup {broken.name} has problems: failed: postgres"),
    ]:
        lines = []
        assert _upgrade(pg_live_shaped.url, lines, backup_root=root) == 1
        assert _result(lines)["error"] == error
        assert _version(pg_live_shaped.url) is None
    assert _upgrade(pg_live_shaped.url, backup_root=good) == 0
    assert _version(pg_live_shaped.url) == [HEAD]
    # An empty database holds nothing a backup could repair.
    assert _upgrade(pg_database.url, backup_root=none, allow_empty=True) == 0


def test_an_impossible_backup_stamp_is_reported_and_is_no_usable_backup_only_for_a_write(pg_database, pg_live_shaped,
                                                                                         tmp_path):
    root = tmp_path / "odd"
    # Named like a stamp, so newest() takes it, but month 13 is no time.
    make_backup(root, age_h=1).rename(root / "20261340T031502Z")
    line = "migrate: preflight: newest backup: 20261340T031502Z's stamp is not a time ("
    assert _upgrade(pg_database.url, allow_empty=True) == 0
    for version in (HEAD, "0099_from_a_later_tree"):
        _execute(pg_database.url, f"UPDATE alembic_version SET version_num = '{version}'")
        lines = []
        assert _upgrade(pg_database.url, lines, backup_root=root) == 0, "\n".join(lines)
        assert [entry for entry in lines if entry.startswith(line)]
    lines = []
    assert migrate.run(pg_live_shaped.url, command="check", backup_root=root, log=lines.append) == 0
    assert [entry for entry in lines if entry.startswith(line)]
    lines = []
    assert _upgrade(pg_live_shaped.url, lines, backup_root=root) == 1
    assert _result(lines)["error"].startswith("no usable backup: 20261340T031502Z's stamp is not a time (")
    assert _version(pg_live_shaped.url) is None
    assert _upgrade(pg_live_shaped.url, backup_root=root, skip_backup_check=True) == 0
    assert _version(pg_live_shaped.url) == [HEAD]


def test_a_backup_stamped_in_the_future_is_not_usable(pg_database, pg_live_shaped, tmp_path):
    root = tmp_path / "ahead"
    # Stamped a day after the clock: a skewed clock, or a directory made by hand. It sorts above every real run.
    future = make_backup(root, age_h=-24)
    ignored = f"migrate: preflight: ignored backup {future.name}, stamped 24.0 h ahead of the clock"
    lines = []
    assert _upgrade(pg_live_shaped.url, lines, backup_root=root) == 1
    assert _result(lines)["error"] == (f"no usable backup: {future.name} is stamped 24.0 h ahead of the clock, and "
                                       f"{root} holds no other backup")
    assert ignored in lines and "migrate: preflight: newest backup: none" in lines
    assert _version(pg_live_shaped.url) is None
    # Off the write path it is reported, not used.
    assert _upgrade(pg_database.url, allow_empty=True) == 0
    lines = []
    assert _upgrade(pg_database.url, lines, backup_root=root) == 0, "\n".join(lines)
    assert ignored in lines
    # Beside a real backup, the real one is the newest and the write uses it.
    real = make_backup(root, age_h=2)
    lines = []
    assert _upgrade(pg_live_shaped.url, lines, backup_root=root) == 0, "\n".join(lines)
    assert ignored in lines and f"migrate: preflight: newest backup {real.name}, 2.0 h old, no problems" in lines
    assert _version(pg_live_shaped.url) == [HEAD]
    # newest() skips it only given a clock; readiness, which passes none, sees it and warns. Five minutes are skew.
    assert backup_manifest.newest(root, now=NOON) == real and backup_manifest.newest(root) == future
    skewed = make_backup(tmp_path / "skewed", age_h=-4 / 60)
    assert backup_manifest.newest(skewed.parent, now=NOON) == skewed and backup_manifest.ahead(skewed.parent,
                                                                                               now=NOON) == []


DATA_REVISION = _revision("0002_data", HEAD, 'op.execute("UPDATE tasks SET priority = 0 WHERE priority IS NULL")',
                          kind='KIND = "data"')


def test_an_empty_database_with_a_pending_data_revision_builds_without_a_backup_inside_the_window(
        pg_live_shaped, pg_database, tmp_path):
    location = _tree(tmp_path, "tree", {"0002_data.py": DATA_REVISION})
    no_backup = tmp_path / "no-backups"
    # The database with tables at the data revision's parent.
    assert _upgrade(pg_live_shaped.url) == 0 and _version(pg_live_shaped.url) == [HEAD]
    for database, code in ((pg_database, 0), (pg_live_shaped, 75)):
        dump = create_engine(database.url, poolclass=NullPool, connect_args={"application_name": "pg_dump"}).connect()
        lines = []
        try:
            assert _upgrade(database.url, lines, allow_empty=True, script_location=location, backup_root=no_backup,
                            now=IN_THE_WINDOW) == code, "\n".join(lines)
        finally:
            dump.close()
    assert _version(pg_database.url) == ["0002_data"] and _version(pg_live_shaped.url) == [HEAD]


def test_a_data_revision_needs_a_backup_newer_than_six_hours(pg_server, pg_database, tmp_path):
    location = _tree(tmp_path, "tree", {"0002_data.py": DATA_REVISION})
    seven, five = make_backup(tmp_path / "seven", age_h=7), make_backup(tmp_path / "five", age_h=5)
    assert _upgrade(pg_database.url, allow_empty=True) == 0
    lines = []
    assert _upgrade(pg_database.url, lines, script_location=location, backup_root=seven.parent) == 75
    assert _result(lines)["error"] == "not now: a data revision needs a backup newer than 6 h (newest: 7.0 h)"
    assert _upgrade(pg_database.url, script_location=location, backup_root=seven.parent, skip_backup_check=True) == 0
    assert _version(pg_database.url) == ["0002_data"]
    with pg_server.database() as other:
        assert _upgrade(other.url, allow_empty=True) == 0
        assert _upgrade(other.url, script_location=location, backup_root=five.parent) == 0
        assert _version(other.url) == ["0002_data"]


def test_kind_of_reads_exactly_one_kind_line():
    assert kind_of('"""A revision."""\nKIND = "data"\n') == "data"
    assert kind_of('KIND = "schema"') == "schema"
    for text_ in ("KIND = 'data'\n", 'KIND: str = "data"\n', '    KIND = "data"\n', 'KIND = "data"\nKIND = "data"\n',
                  'KIND = "data"\nKIND = "schema"\n', 'KIND = "other"\n', "", 'KIND = "data" \n'):
        assert kind_of(text_) is None, text_


@pytest.mark.parametrize("kind", ["KIND = 'data'", "# no kind line"])
def test_a_pending_revision_without_a_valid_kind_is_refused(pg_database, tmp_path, kind):
    assert _upgrade(pg_database.url, allow_empty=True) == 0
    location = _tree(tmp_path, "tree", {"0002_kindless.py": _revision("0002_kindless", HEAD, ADD_COLUMN, kind=kind)})
    for skip in (False, True):
        lines = []
        assert _upgrade(pg_database.url, lines, script_location=location, skip_backup_check=skip) == 1
        assert _result(lines)["error"] == '0002_kindless has no valid KIND line (KIND = "schema" or KIND = "data")'
        assert _version(pg_database.url) == [HEAD]


def test_a_failed_revision_prints_its_sqlstate_but_no_row_values(pg_database, tmp_path):
    assert _upgrade(pg_database.url, allow_empty=True) == 0
    location = _tree(tmp_path, "tree", {"0002_values.py": _revision("0002_values", HEAD, (
        'op.execute(sa.text("INSERT INTO tasks (id, idempotency_key) VALUES (\'wp26-a\', :value), (\'wp26-b\', '
        '\'other\')").bindparams(value="wp26-row-value"))\n'
        'op.execute(sa.text("UPDATE tasks SET idempotency_key = :value WHERE id = \'wp26-b\'").bindparams('
        'value="wp26-row-value"))'), kind='KIND = "data"')})
    lines = []
    assert _upgrade(pg_database.url, lines, script_location=location) == 1
    error = _result(lines)["error"]
    assert error == ('0002_values failed: 23505 duplicate key value violates unique constraint '
                     '"ix_tasks_idempotency_key"')
    assert error in [line.removeprefix("migrate: ") for line in lines]
    for line in lines:
        assert "wp26-row-value" not in line and "[SQL:" not in line and "[parameters:" not in line, line


def test_a_failed_run_still_prints_the_result_line(pg_database, tmp_path):
    lines = []
    assert migrate.run("sqlite+aiosqlite:///nowhere.db", log=lines.append) == 1
    assert _result(lines) == {"state": None, "from": None, "to": None, "applied": [], "drift": [],
                              "error": "sqlite+aiosqlite is not a postgresql URL"}
    assert _upgrade(pg_database.url, allow_empty=True) == 0
    location = _tree(tmp_path, "tree", {
        "0002_add.py": _revision("0002_add", HEAD, ADD_COLUMN),
        "0003_fails.py": _revision("0003_fails", "0002_add", 'raise RuntimeError("no")')})
    lines = []
    assert _upgrade(pg_database.url, lines, script_location=location) == 1
    result = _result(lines)
    assert (result["state"], result["from"], result["to"]) == ("behind", HEAD, "0002_add")
    assert result["error"] == "0003_fails failed: RuntimeError: no"


def test_check_never_writes(pg_live_shaped, monkeypatch):
    sessions = []
    real = migrate._state

    def observed(conn, chain):
        sessions.append(conn.exec_driver_sql("SHOW transaction_read_only").scalar_one())
        conn.rollback()
        return real(conn, chain)

    monkeypatch.setattr(migrate, "_state", observed)
    lines = []
    assert migrate.run(pg_live_shaped.url, command="check", json_result=True, log=lines.append) == 0, "\n".join(lines)
    assert sessions == ["on"]
    assert lines[0] == "migrate: state pre_alembic none"
    assert f"migrate: the gate would stamp {migrate.BASELINE}" in lines
    assert _result(lines)["state"] == "pre_alembic"
    assert _version(pg_live_shaped.url) is None
    _execute(pg_live_shaped.url, "ALTER TABLE tasks ADD COLUMN wp26_extra integer")
    lines = []
    assert migrate.run(pg_live_shaped.url, command="check", log=lines.append) == 3
    assert "migrate: gate: + column tasks.wp26_extra: integer" in lines and not lines[-1].startswith("migrate-result")


@pytest.mark.parametrize("database", migrate.TEMPORAL_DATABASES)
def test_temporal_databases_are_refused(monkeypatch, database):
    _no_engine(monkeypatch)
    lines = []
    assert migrate.run(f"postgresql://temporal@127.0.0.1:15432/{database}", log=lines.append) == 1
    assert _result(lines)["error"] == f"{database} is Temporal's database; only rmp_db is migrated here"


def test_a_non_postgres_url_is_refused(monkeypatch):
    _no_engine(monkeypatch)
    lines = []
    # The suite's own DATABASE_URL, SQLite's.
    assert migrate.run(os.environ["DATABASE_URL"], url_from_env=True, log=lines.append) == 1
    assert _result(lines)["error"] == "sqlite+aiosqlite is not a postgresql URL"


def test_no_url_is_an_error_and_the_env_file_is_never_read(monkeypatch, capsys):
    _no_engine(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    opened = []
    real_open = open

    def recording_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", recording_open)
    assert migrate.main(["upgrade"]) == 1
    out = capsys.readouterr().out
    assert "migrate: no database: give --url or set DATABASE_URL" in out
    assert not [path for path in opened if "rmp.env" in path]
    for module in ("migrate.py", "fingerprint.py", "revision_kind.py"):
        tree = ast.parse((REPO / "app" / "db" / module).read_text())
        imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        assert not imported & {"app.db.database", "app.config"}, module


def test_a_revision_id_fits_the_version_table(tmp_path, capsys):
    location = _tree(tmp_path, "tree", {})
    path = migrate.new_revision("Add the events actor column for the audit trail", script_location=location)
    assert path == location / "versions" / "0002_add_the_events_actor_column.py" and path.is_file()
    assert len(path.stem) <= migrate.REVISION_ID_MAX
    source = path.read_text()
    assert "revision = '0002_add_the_events_actor_column'" in source and "down_revision = '0001_baseline'" in source
    assert kind_of(source) == "schema"
    assert ScriptDirectory(str(location)).get_heads() == ["0002_add_the_events_actor_column"]
