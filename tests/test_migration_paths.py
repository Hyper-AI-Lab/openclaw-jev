"""The paths a schema change may take (specs/WP-26.md). Until WP-26 batch 6 lands the schema conventions, ordering
rule 44 freezes rmp_db's schema: the baseline is the only revision, and the instructions Aura's sessions read say so
(WP26-30)."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VERSIONS = REPO / "app" / "db" / "migrations" / "versions"
SCHEMA_FREEZE = (
    "Until the schema conventions land (WP-26 batch 6), no PR changes the database schema: no Alembic revision in "
    "`app/db/migrations/versions/`, and no change in `app/db/models.py` to a table, to a column's presence, type, "
    "nullability or server default, to an index or to a constraint (ordering rule 44). Python-side `default=` and "
    "`onupdate=` arguments are not schema and are not frozen.")


def test_the_instructions_name_the_schema_freeze():
    for name in ("CLAUDE.md", "CONTRIBUTING.md"):
        assert SCHEMA_FREEZE in " ".join((REPO / name).read_text(encoding="utf-8").split()), name


def test_no_revision_beyond_the_baseline_until_wp26_batch_6():
    # A revision would pass every other test and ship before the deploy has a migration step to run it (WP-26 D-3).
    # A model change without one fails test_the_models_differ_from_head_by_the_known_drift_only where compare_metadata
    # sees it; a server default, a CHECK or an index predicate only the instructions above and review hold until
    # batch 6's models-against-head test. Batch 6, which adds 0002-0005, removes this test.
    assert sorted(path.name for path in VERSIONS.glob("*.py")) == ["0001_baseline.py"]
