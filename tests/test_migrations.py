"""Schema migrations of rmp_db (specs/WP-26.md batch 1): the catalog fingerprint.

The Postgres tests run on tests/postgres_fixture.py's throwaway server, never the live cluster.
"""
import json
import re
from pathlib import Path
from typing import List, Tuple

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.db import fingerprint

pytest_plugins = ["tests.postgres_fixture"]

REPO = Path(__file__).resolve().parents[1]
FINGERPRINT_SQL = REPO / "app" / "db" / "fingerprint.sql"


def _engine(url):
    return create_engine(url, poolclass=NullPool)


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
