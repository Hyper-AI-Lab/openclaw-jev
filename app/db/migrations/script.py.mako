"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = None
depends_on = None
# Read from this line by app/db/revision_kind.py: "schema", or "data" for a revision that changes rows.
KIND = "schema"


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    raise RuntimeError("forward only: rmp_db is never downgraded (ordering rule 17); fix forward with a new revision")
