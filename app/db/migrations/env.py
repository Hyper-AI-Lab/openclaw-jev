"""Alembic's environment for rmp_db (specs/WP-26.md WP26-04, D-16): online only, on the connection app/db/migrate.py
passes in ``config.attributes["connection"]``, each revision in a transaction of its own.

It imports ``app.db.models`` and nothing else of RMP's: no settings, no environment file, no engine of the
application's (``app/db/database.py`` builds one at import, from ``/etc/rmp/rmp.env`` when ``DATABASE_URL`` is unset).
"""
from alembic import context

from app.db.models import Base

# The indexes only the database declares (drift D11): the baseline creates them and the catalog fingerprint checks
# their definitions, which compare_metadata cannot (GIN expressions, a partial index); no model declares them. Each
# was created by app/db/database.py's _MIGRATIONS, at the line given.
DB_ONLY_INDEXES = frozenset({
    "ix_tasks_goal_fts",  # database.py:121
    "ix_task_registry_fts",  # database.py:122
    "ix_task_messages_fts",  # database.py:123
    "ix_memory_items_content_fts",  # database.py:126
    "ix_vector_outbox_pending",  # database.py:127
    "ix_events_type_time",  # database.py:128
    "ix_dm_chunks_fts",  # database.py:136
    "ix_dm_sections_fts",  # database.py:137
    "ix_dm_documents_fts",  # database.py:138
})
# The tables the database keeps after their model is gone (ordering rule 17 keeps every table): none yet.
RETAINED_TABLES: frozenset = frozenset()


def include_object(obj, name, type_, reflected, compare_to):
    """What compare_metadata compares: not the database's own indexes, nor the tables it keeps without a model."""
    if reflected and compare_to is None:
        if type_ == "index" and name in DB_ONLY_INDEXES:
            return False
        if type_ == "table" and name in RETAINED_TABLES:
            return False
    return True


if context.is_offline_mode():
    raise RuntimeError("rmp_db's migrations run online only, through python -m app.db.migrate")

config = context.config
context.configure(
    connection=config.attributes["connection"],
    target_metadata=Base.metadata,
    transaction_per_migration=True,
    compare_type=True,
    compare_server_default=False,
    include_object=include_object,
    on_version_apply=config.attributes.get("on_version_apply", ()),
)
with context.begin_transaction():
    context.run_migrations()
