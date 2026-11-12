"""The baseline: rmp_db's schema as production had it before Alembic, frozen (specs/WP-26.md WP26-02, D-1).

``baseline.sql`` was rendered once from the verified nightly backup its header names, by ``pg_restore --schema-only
--no-owner --no-privileges --no-comments --no-tablespaces``, with only the session settings, psql meta-commands and
the unused ``vector`` extension removed. A new database gets this schema here; production, which has it already, is
stamped at this revision once app/db/migrate.py's gate has found its catalog equal to ``baseline.json``.
"""
from pathlib import Path

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None
KIND = "schema"


def upgrade() -> None:
    # psycopg2 sends the whole file as one simple query.
    op.get_bind().exec_driver_sql((Path(__file__).parents[1] / "baseline.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    raise RuntimeError("forward only: rmp_db is never downgraded (ordering rule 17); fix forward with a new revision")
