"""Controlled Alembic migration runner for UIMRS.

Why this module exists
----------------------
UIMRS databases created before Task 6 already contain every baseline table but
have no ``alembic_version`` table. Running ``alembic upgrade head`` directly on
such a database would try to create the baseline tables again and fail.

``prepare_database`` handles the three real situations safely:

1. **Completely empty database** — runs the full revision chain (baseline +
   explainability columns). A brand-new installation therefore gets a fully
   versioned schema.
2. **Existing pre-Alembic database** — validates that the baseline schema is
   present, *stamps* it at the baseline revision (no table is recreated), then
   upgrades to head. If the database was already upgraded manually but never
   stamped, it is stamped at head instead of migrating twice.
3. **Already versioned database** — a normal ``upgrade head``; re-running is a
   no-op.

The database URL is always supplied by the caller or resolved from the
application configuration. No development path is hard-coded here.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.core.config import resolve_database_url
from app.db_guard import (
    AUDIT_LOG_TABLE,
    BASELINE_SCHEMA,
    DEPARTMENT_ROUTING_COLUMNS,
    EXPLAINABILITY_COLUMNS,
    ISSUE_ROUTING_COLUMNS,
    check_sqlite_integrity,
    sqlite_path_from_url,
    validate_schema,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
ALEMBIC_DIR = PROJECT_ROOT / "backend" / "alembic"

BASELINE_REVISION = "0001_baseline"
EXPLAINABILITY_REVISION = "0002_ai_explainability"
DEPARTMENT_ROUTING_REVISION = "0003_department_routing"
HEAD_REVISION = "0005_image_data_bytea"


class MigrationError(RuntimeError):
    """Raised when a database cannot be migrated safely."""


@dataclass(frozen=True)
class MigrationResult:
    database_url: str
    state: str
    revision: str | None


def make_alembic_config(database_url: str) -> Config:
    """Build an Alembic config bound to an explicit database URL."""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.attributes["database_url"] = database_url
    return config


def current_revision(database_url: str) -> str | None:
    """Return the stored Alembic revision, or ``None`` when unversioned."""
    path = sqlite_path_from_url(database_url)
    if path is not None and not path.exists():
        # A status check must not create an empty SQLite file as a side effect.
        return None
    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        if "alembic_version" not in tables:
            return None
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).first()
        return str(row[0]) if row else None
    finally:
        engine.dispose()


def _check_integrity(database_url: str) -> None:
    path = sqlite_path_from_url(database_url)
    if path is not None and path.exists():
        check_sqlite_integrity(database_url)


def _prepare_existing_unversioned(database_url: str, config: Config) -> MigrationResult:
    """Stamp a pre-Alembic database without recreating any of its tables."""
    engine = create_engine(database_url)
    try:
        # Refuse to stamp a database that is not recognisably the baseline
        # schema. A legacy/foreign database must never be adopted by accident.
        validate_schema(engine, BASELINE_SCHEMA)
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        issue_columns = {
            column["name"] for column in inspector.get_columns("issues")
        }
        department_columns = {
            column["name"] for column in inspector.get_columns("departments")
        }
    finally:
        engine.dispose()

    def _reject_partial(present: set[str], expected: tuple[str, ...], label: str) -> None:
        if present and present != set(expected):
            missing = sorted(set(expected) - present)
            raise MigrationError(
                f"Database has a partially applied {label} schema. Refusing to "
                f"guess a revision. Missing columns: {', '.join(missing)}. "
                "Restore a backup or apply the missing columns manually."
            )

    explain_present = set(EXPLAINABILITY_COLUMNS) & issue_columns
    issue_routing_present = set(ISSUE_ROUTING_COLUMNS) & issue_columns
    department_routing_present = set(DEPARTMENT_ROUTING_COLUMNS) & department_columns

    _reject_partial(explain_present, EXPLAINABILITY_COLUMNS, "explainability")
    _reject_partial(issue_routing_present, ISSUE_ROUTING_COLUMNS, "issue routing")
    _reject_partial(
        department_routing_present, DEPARTMENT_ROUTING_COLUMNS, "department routing"
    )

    has_explainability = set(EXPLAINABILITY_COLUMNS).issubset(issue_columns)
    has_issue_routing = set(ISSUE_ROUTING_COLUMNS).issubset(issue_columns)
    has_department_routing = set(DEPARTMENT_ROUTING_COLUMNS).issubset(
        department_columns
    )
    has_audit_log = AUDIT_LOG_TABLE in tables

    if has_audit_log:
        if has_explainability and has_issue_routing and has_department_routing:
            # Every revision is already applied but the database was never
            # stamped. Record the truth instead of recreating anything.
            command.stamp(config, "head")
            return MigrationResult(
                database_url, "stamped_head", current_revision(database_url)
            )
        raise MigrationError(
            "Database has an audit table but is missing earlier schema "
            "revisions. Refusing to guess a revision. Restore a backup or apply "
            "the missing revisions manually."
        )

    if has_explainability and has_issue_routing and has_department_routing:
        # Task 8 schema already exists (manual upgrade) but the database was
        # never stamped. Only the Task 10 audit revision remains.
        command.stamp(config, DEPARTMENT_ROUTING_REVISION)
        command.upgrade(config, "head")
        return MigrationResult(
            database_url, "stamped_department_routing", current_revision(database_url)
        )

    if has_explainability and not (has_issue_routing or has_department_routing):
        # Manually upgraded to 0002 but never stamped; 0003 and 0004 remain.
        command.stamp(config, EXPLAINABILITY_REVISION)
        command.upgrade(config, "head")
        return MigrationResult(
            database_url, "stamped_explainability", current_revision(database_url)
        )

    if not (has_explainability or has_issue_routing or has_department_routing):
        command.stamp(config, BASELINE_REVISION)
        command.upgrade(config, "head")
        return MigrationResult(
            database_url, "stamped_baseline", current_revision(database_url)
        )

    raise MigrationError(
        "Database has an inconsistent mix of explainability and routing columns. "
        "Refusing to guess a revision. Restore a backup or apply the missing "
        "revisions manually."
    )


def prepare_database(database_url: str | None = None) -> MigrationResult:
    """Bring a database to the current schema revision using Alembic.

    Safe to call repeatedly. Existing rows are never rewritten, and no table is
    recreated on an existing database.
    """
    url = database_url or resolve_database_url()
    _check_integrity(url)
    config = make_alembic_config(url)

    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    if not tables:
        command.upgrade(config, "head")
        return MigrationResult(url, "initialized", current_revision(url))

    if "alembic_version" in tables:
        previous = current_revision(url)
        if previous:
            command.upgrade(config, "head")
            revision = current_revision(url)
            state = "already_current" if revision == previous else "upgraded"
            return MigrationResult(url, state, revision)
        # An empty version table is an inconsistent/unfinished state; fall back
        # to the safe pre-Alembic stamping path.

    return _prepare_existing_unversioned(url, config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply controlled UIMRS Alembic migrations."
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy URL. Defaults to the application DATABASE_URL.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print the current revision without changing the database.",
    )
    args = parser.parse_args(argv)

    url = args.database_url or resolve_database_url()
    if args.status:
        print(f"database: {url}")
        print(f"revision: {current_revision(url)}")
        return 0

    result = prepare_database(url)
    print(f"database: {result.database_url}")
    print(f"state:    {result.state}")
    print(f"revision: {result.revision}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI script
    raise SystemExit(main())
