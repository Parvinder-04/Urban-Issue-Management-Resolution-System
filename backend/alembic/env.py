"""Alembic migration environment for UIMRS.

Database selection is environment-driven:

* ``Config.attributes["database_url"]`` (highest precedence) — used by the
  controlled migration runner and by the isolated test suite.
* ``Config.attributes["connection"]`` — an existing SQLAlchemy connection, if a
  caller already owns one.
* The application's configured ``DATABASE_URL`` (see
  ``app.core.config.resolve_database_url``) — the normal CLI path.

No development database path is hard-coded in this file or in any revision.
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# Make the ``app`` package importable when Alembic is invoked from the project
# root or from an arbitrary working directory.
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import resolve_database_url  # noqa: E402
from app.database import Base  # noqa: E402
from app import models  # noqa: E402,F401  (registers every table on Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the URL for this run without hard-coding any database path."""
    explicit = config.attributes.get("database_url")
    if explicit:
        return str(explicit)
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    return resolve_database_url()


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (``--sql`` mode)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_with_connection(connection)
        return

    connectable = create_engine(_database_url(), poolclass=pool.NullPool)
    try:
        with connectable.connect() as active_connection:
            _run_with_connection(active_connection)
    finally:
        connectable.dispose()


def _run_with_connection(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite cannot alter most table/column definitions in place. Batch
        # mode lets Alembic recreate a table safely when an operation needs it
        # while leaving simple ADD COLUMN operations as native ALTERs.
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
