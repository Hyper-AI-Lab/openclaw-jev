"""tests/postgres_fixture.py: one throwaway server per run, unprivileged databases in Berlin time, never the live
cluster, and a skip with its reason, or a failure when a server is required, when there is none."""
import ast
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

pytest_plugins = ["tests.postgres_fixture", "pytester"]

TESTS = Path(__file__).resolve().parent
FIXTURES = {"pg_server", "pg_database", "pg_live_shaped"}


@pytest.fixture
def plugin(request):
    """The fixture module, as pytest registered it."""
    return request.config.pluginmanager.get_plugin("tests.postgres_fixture")


def _one(url, query):
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return [tuple(row) for row in conn.execute(text(query))]
    finally:
        engine.dispose()


def test_the_server_runs_in_berlin_time_with_c_utf8(pg_database):
    assert _one(pg_database.url, "SHOW TimeZone") == [("Europe/Berlin",)]
    assert _one(pg_database.url, "SELECT datcollate, datctype FROM pg_database WHERE datname = current_database()") \
        == [("C.UTF-8", "C.UTF-8")]


def test_the_owner_role_is_not_a_superuser(pg_database):
    assert _one(pg_database.url, "SELECT current_user, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin "
                                 "FROM pg_roles WHERE rolname = current_user") == [
        ("rmp_it_owner", False, False, False, True)]
    assert _one(pg_database.url, "SELECT pg_get_userbyid(datdba) FROM pg_database "
                                 "WHERE datname = current_database()") == [("rmp_it_owner",)]


def test_a_live_shaped_database_belongs_to_the_owner_role(pg_live_shaped):
    owners = _one(pg_live_shaped.url, "SELECT tableowner, count(*) FROM pg_tables WHERE schemaname = 'public' "
                                      "GROUP BY tableowner")
    assert owners == [("rmp_it_owner", 20)]
    assert _one(pg_live_shaped.url, "SELECT to_regclass('public.alembic_version')") == [(None,)]


def test_every_url_points_at_the_fixtures_own_server(pg_server, pg_database, pg_live_shaped):
    for database in (pg_database, pg_live_shaped):
        for url in (database.url, database.async_url, database.admin_url):
            parsed = make_url(url)
            assert parsed.database == database.name and database.name.startswith("rmp_it_")
            if pg_server.base is not None:
                assert parsed.host is None and parsed.query == {"host": pg_server.base, "port": "55440"}
                assert Path(pg_server.base, ".s.PGSQL.55440").is_socket()
            else:
                assert (parsed.host, parsed.port) == (pg_server.host, pg_server.port)
    assert not pg_server.owns("postgresql+psycopg2://rmp@/rmp_db?host=/run/postgresql&port=5432")
    assert not pg_server.owns("postgresql+psycopg2://rmp@127.0.0.1:5432/rmp_db")


@pytest.mark.parametrize("url, reason", [
    ("postgresql://postgres@127.0.0.1:5432/postgres", "its port 5432 is one of this host's service ports"),
    ("postgresql://postgres@10.0.0.5:15432/postgres", "its host is '10.0.0.5'"),
    ("postgresql://postgres@127.0.0.1:15432/rmp_db", "it names rmp_db"),
    ("postgresql://postgres@/postgres?host=/run/postgresql", "its host is None"),
])
def test_an_external_server_on_port_5432_is_refused(plugin, monkeypatch, url, reason):
    monkeypatch.setenv("RMP_PG_IT_URL", url)
    monkeypatch.delenv("RMP_PG_IT_REQUIRED", raising=False)
    with pytest.raises(pytest.fail.Exception, match=reason):
        plugin.start()


def test_a_required_server_that_is_missing_fails(plugin, monkeypatch, tmp_path):
    monkeypatch.delenv("RMP_PG_IT_URL", raising=False)
    monkeypatch.setenv("RMP_PG_BIN", str(tmp_path / "no-postgres"))
    with pytest.raises(plugin.Unavailable, match="has no initdb") as missing:
        plugin.start()
    monkeypatch.setenv("RMP_PG_IT_REQUIRED", "1")
    with pytest.raises(pytest.fail.Exception, match="RMP_PG_IT_REQUIRED=1 and no Postgres server: .*has no initdb"):
        plugin.unavailable(str(missing.value))
    monkeypatch.delenv("RMP_PG_IT_REQUIRED")
    with pytest.raises(pytest.skip.Exception, match="no Postgres server: .*has no initdb"):
        plugin.unavailable(str(missing.value))


def test_a_local_server_that_cannot_start_skips_with_its_reason_unless_required(plugin, monkeypatch):
    # The fake initdb lives where the cluster's own user can run it: nobody when the tests run as root.
    fakes = tempfile.mkdtemp(prefix="rmp-fake-pg-bin-")
    try:
        os.chmod(fakes, 0o755)
        initdb = Path(fakes, "initdb")
        initdb.write_text("#!/bin/sh\necho 'initdb: the fake data directory is not empty'\nexit 1\n")
        initdb.chmod(0o755)
        monkeypatch.delenv("RMP_PG_IT_URL", raising=False)
        monkeypatch.setenv("RMP_PG_BIN", fakes)
        before = {p.name for p in Path(tempfile.gettempdir()).iterdir() if p.name.startswith("rmp-pg-")}
        with pytest.raises(plugin.Unavailable) as failed:
            plugin.start()
        assert str(failed.value) == "initdb exited 1: initdb: the fake data directory is not empty"
        assert {p.name for p in Path(tempfile.gettempdir()).iterdir() if p.name.startswith("rmp-pg-")} == before
        monkeypatch.delenv("RMP_PG_IT_REQUIRED", raising=False)
        with pytest.raises(pytest.skip.Exception, match="initdb exited 1: initdb: the fake data directory"):
            plugin.unavailable(str(failed.value))
        monkeypatch.setenv("RMP_PG_IT_REQUIRED", "1")
        with pytest.raises(pytest.fail.Exception, match="initdb exited 1: initdb: the fake data directory"):
            plugin.unavailable(str(failed.value))
    finally:
        Path(fakes, "initdb").unlink(missing_ok=True)
        os.rmdir(fakes)


# A test module that needs a database, run by an inner pytest session through the fixture itself.
USES_A_DATABASE = """
pytest_plugins = ["tests.postgres_fixture"]


def test_needs_a_database(pg_database):
    assert pg_database.name
"""


@pytest.mark.parametrize("required", [False, True], ids=["optional", "required"])
@pytest.mark.parametrize("server", ["initdb fails", "no binaries"])
def test_a_test_using_the_fixture_skips_without_a_server_or_errors_when_one_is_required(pytester, monkeypatch, server,
                                                                                       required):
    # The fake initdb lives where the cluster's own user can run it: nobody when the tests run as root.
    fakes = tempfile.mkdtemp(prefix="rmp-fake-pg-bin-")
    try:
        os.chmod(fakes, 0o755)
        if server == "initdb fails":
            bin_dir = fakes
            Path(fakes, "initdb").write_text("#!/bin/sh\necho 'initdb: the fake data directory is not empty'\nexit 1\n")
            Path(fakes, "initdb").chmod(0o755)
            reason = "initdb exited 1: initdb: the fake data directory is not empty"
        else:
            bin_dir = os.path.join(fakes, "missing")
            reason = f"RMP_PG_IT_URL is not set and {bin_dir} has no initdb"
        monkeypatch.delenv("RMP_PG_IT_URL", raising=False)
        monkeypatch.setenv("RMP_PG_BIN", bin_dir)
        if required:
            monkeypatch.setenv("RMP_PG_IT_REQUIRED", "1")
        else:
            monkeypatch.delenv("RMP_PG_IT_REQUIRED", raising=False)
        clusters = {p.name for p in Path(tempfile.gettempdir()).iterdir() if p.name.startswith("rmp-pg-")}
        pytester.makepyfile(test_inner=USES_A_DATABASE)
        # The inner session is pinned to its own directory, whatever TMPDIR is: it never loads tests/conftest.py, whose
        # unconfigure would turn this session's production guard off and remove its temp root. pytest-asyncio, which it
        # does not need, would warn about its settings.
        result = pytester.runpytest_inprocess("-rs", "-p", "no:cacheprovider", "-p", "no:asyncio",
                                              f"--rootdir={pytester.path}", f"--confcutdir={pytester.path}")
        if required:
            result.assert_outcomes(errors=1)
            assert f"RMP_PG_IT_REQUIRED=1 and no Postgres server: {reason}" in result.stdout.str()
        else:
            result.assert_outcomes(skipped=1)
            assert f"no Postgres server: {reason}" in result.stdout.str()
        assert {p.name for p in Path(tempfile.gettempdir()).iterdir() if p.name.startswith("rmp-pg-")} == clusters
    finally:
        Path(fakes, "initdb").unlink(missing_ok=True)
        os.rmdir(fakes)


def test_the_first_directory_nobody_cannot_traverse_is_named(plugin):
    modes = {"/": (0o755, 0), "/srv": (0o755, 0), "/srv/aura-code": (0o711, 0), "/srv/aura-code/cache": (0o700, 0),
             "/srv/aura-code/cache/tmp": (0o700, 0)}

    def fake_stat(path):
        mode, uid = modes[path]
        return SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=uid)

    path = "/srv/aura-code/cache/tmp/rmp-pg-x"
    assert plugin.untraversable(path, stat=fake_stat) == "/srv/aura-code/cache"
    modes["/srv/aura-code/cache"] = (0o700, plugin.NOBODY)
    assert plugin.untraversable(path, stat=fake_stat) == "/srv/aura-code/cache/tmp"
    modes["/srv/aura-code/cache/tmp"] = (0o711, 0)
    assert plugin.untraversable(path, stat=fake_stat) is None


def _uses_a_postgres_fixture(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if {arg.arg for arg in node.args.args} & FIXTURES:
                return True
    return False


def _declares_the_plugin(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytest_plugins"
                                                for t in node.targets):
            return "tests.postgres_fixture" in ast.literal_eval(node.value)
    return False


def test_postgres_modules_register_the_fixture_as_a_plugin():
    users, imports = [], []
    for path in sorted(TESTS.rglob("*.py")):
        if path.name == "postgres_fixture.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        name = str(path.relative_to(TESTS))
        if _uses_a_postgres_fixture(tree):
            users.append(name)
            assert _declares_the_plugin(tree), f"{name} uses a Postgres fixture without pytest_plugins"
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "tests.postgres_fixture":
                imports.append(name)
            if isinstance(node, ast.ImportFrom) and node.module == "tests" and any(
                    alias.name == "postgres_fixture" for alias in node.names):
                imports.append(name)
            if isinstance(node, ast.Import) and any(alias.name == "tests.postgres_fixture" for alias in node.names):
                imports.append(name)
    assert "test_postgres_fixture.py" in users
    assert imports == [], "import nothing from tests.postgres_fixture; declare it in pytest_plugins"
