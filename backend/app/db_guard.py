"""Database safety guards used at startup and by maintenance tooling.

Responsibilities:
  1. Validate that an existing database already contains the required tables and
     columns; fail with an explicit error instead of running against an
     incompatible schema.
  2. Run ``PRAGMA integrity_check`` read-only on SQLite databases.
  3. Create safe working copies through the SQLite backup API, never
     overwriting an existing destination.

This module deliberately does not create, alter or migrate schema. Schema
creation and migration are owned by Alembic (see ``app.db_migrate``); this
module only validates an existing database.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Mapping

from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

SQLITE_PREFIX = "sqlite:///"

# Schema as it existed before Task 6. The Alembic baseline revision (0001) is
# stamped against exactly this shape, so the migration runner can recognise a
# pre-Alembic database without recreating it.
BASELINE_SCHEMA: Mapping[str, tuple[str, ...]] = {
    "users": (
        "id", "name", "email", "password_hash", "role",
        "department_id", "preferred_language", "created_at",
    ),
    "departments": (
        "id", "name", "handles_category", "contact_email",
        "city", "source_agency",
    ),
    "issues": (
        "id", "ticket_number", "title", "description", "category",
        "category_confidence", "category_source",
        "priority_score", "priority_level", "status",
        "latitude", "longitude", "address_text",
        "duplicate_of_issue_id", "duplicate_count", "hotspot_cluster_id",
        "reporter_id", "department_id", "assigned_officer_id",
        "created_at", "updated_at", "resolved_at",
        "city", "is_historical",
        "source_dataset", "source_record_id", "source_status",
        "source_agency", "source_category", "source_subcategory",
    ),
    "issue_images": (
        "id", "issue_id", "image_url", "blur_score", "brightness",
        "resolution_width", "resolution_height", "passed_verification",
        "perceptual_hash", "uploaded_at",
    ),
    "issue_status_updates": (
        "id", "issue_id", "updated_by_id", "old_status",
        "new_status", "comment", "created_at",
    ),
}

# Columns added by Task 6 revision 0002. They are nullable so historical rows
# remain valid without any back-fill.
EXPLAINABILITY_COLUMNS: tuple[str, ...] = (
    "category_model_version",
    "category_needs_review",
    "category_review_reason",
    "category_top_alternatives",
    "category_score_kind",
    "priority_reasons",
)

# Columns added by Task 8 revision 0003. Department routing metadata is
# nullable/safe-defaulted; the issue routing outcome stays NULL for historical
# rows, which are never re-routed.
DEPARTMENT_ROUTING_COLUMNS: tuple[str, ...] = (
    "short_name",
    "jurisdiction",
    "contact_purpose",
    "verification_source_url",
    "verification_date",
    "routing_mode",
    "is_active",
)

ISSUE_ROUTING_COLUMNS: tuple[str, ...] = (
    "routing_status",
    "routing_reason",
)

# Table added by Task 10 revision 0004. It is append-only; the guard requires it
# so an application instance can never run without its administrative audit
# trail.
AUDIT_LOG_TABLE = "admin_audit_logs"

AUDIT_LOG_COLUMNS: tuple[str, ...] = (
    "id",
    "actor_user_id",
    "action",
    "entity_type",
    "entity_id",
    "before_state",
    "after_state",
    "reason",
    "city",
    "created_at",
)

# Columns the application actually reads/writes. Provenance and city-scoping
# columns are required so a legacy/foreign schema cannot be used as the India DB.
# The explainability and routing columns are required only after their revisions
# are applied.
REQUIRED_SCHEMA: Mapping[str, tuple[str, ...]] = {
    **BASELINE_SCHEMA,
    "departments": (
        BASELINE_SCHEMA["departments"]
        + ("contact_phone",)
        + DEPARTMENT_ROUTING_COLUMNS
    ),
    "issues": (
        BASELINE_SCHEMA["issues"] + EXPLAINABILITY_COLUMNS + ISSUE_ROUTING_COLUMNS
    ),
    "issue_images": (
        BASELINE_SCHEMA["issue_images"] + ("image_data",)
    ),
    AUDIT_LOG_TABLE: AUDIT_LOG_COLUMNS,
}


class DatabaseSchemaError(RuntimeError):
    """Raised when the database is missing required tables or columns."""


class DatabaseIntegrityError(RuntimeError):
    """Raised when a SQLite database fails its integrity check or cannot be read."""


def sqlite_path_from_url(database_url: str) -> Path | None:
    """Return the on-disk path for a ``sqlite:///`` URL, else ``None``.

    In-memory databases and non-SQLite URLs have no file-level checks.
    """
    if not database_url or not database_url.startswith(SQLITE_PREFIX):
        return None
    raw = database_url[len(SQLITE_PREFIX):]
    if not raw or raw == ":memory:" or raw.startswith(":memory:"):
        return None
    return Path(raw)


def existing_tables(engine) -> set[str]:
    """Return the set of table names currently present in the database."""
    return set(inspect(engine).get_table_names())


def validate_schema(engine, required: Mapping[str, Iterable[str]] = REQUIRED_SCHEMA) -> None:
    """Assert every required table and column exists, or raise DatabaseSchemaError."""
    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    problems: list[str] = []

    for table, columns in required.items():
        if table not in present:
            problems.append(f"missing table: {table}")
            continue
        actual = {column["name"] for column in inspector.get_columns(table)}
        missing = [column for column in columns if column not in actual]
        if missing:
            problems.append(f"table '{table}' is missing columns: {', '.join(missing)}")

    if problems:
        raise DatabaseSchemaError(
            "Database schema is incompatible with this application. "
            "No schema was changed automatically.\n"
            + "\n".join(f"  - {problem}" for problem in problems)
            + "\nProvide a compatible database (or apply a reviewed migration) "
              "and restart."
        )


def check_sqlite_integrity(database_url: str) -> str | None:
    """Run ``PRAGMA integrity_check`` read-only.

    Returns ``"ok"`` for a healthy SQLite database and ``None`` when the URL is
    not a file-backed SQLite database (nothing to check locally).
    """
    path = sqlite_path_from_url(database_url)
    if path is None:
        return None
    if not path.exists():
        raise DatabaseIntegrityError(f"SQLite database file not found: {path}")

    connection = None
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        rows = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as exc:
        raise DatabaseIntegrityError(
            f"SQLite integrity check failed for {path}: {exc}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()

    result = "; ".join(str(row[0]) for row in rows).strip()
    if result.lower() != "ok":
        raise DatabaseIntegrityError(
            f"SQLite integrity check reported problems for {path}: {result}"
        )
    return "ok"


def guard_startup(
    engine,
    database_url: str,
    required: Mapping[str, Iterable[str]] = REQUIRED_SCHEMA,
) -> str:
    """Verify the configured database before the application serves requests.

    Returns:
      * ``"uninitialized"`` — the database has no tables yet; the caller may
        create the initial schema. No existing schema is ever altered.
      * ``"validated"`` — the existing schema matched the requirements.

    Raises:
      * ``DatabaseIntegrityError`` — the SQLite file is unreadable/corrupt.
      * ``DatabaseSchemaError`` — tables or columns are missing.
    """
    try:
        tables = existing_tables(engine)
    except SQLAlchemyError as exc:
        if sqlite_path_from_url(database_url) is not None:
            raise DatabaseIntegrityError(
                f"SQLite database could not be read: {database_url}: {exc}"
            ) from exc
        raise
    if not tables:
        return "uninitialized"
    check_sqlite_integrity(database_url)
    validate_schema(engine, required)
    return "validated"


def safe_backup_database(source: Path | str, destination: Path | str) -> Path:
    """Copy a SQLite database using the backup API.

    The source is opened read-only and the destination must not already exist,
    so neither the source nor an existing working database can be damaged.
    """
    source_path = Path(source)
    destination_path = Path(destination)

    if not source_path.is_file():
        raise FileNotFoundError(f"Source database not found: {source_path}")
    if destination_path.exists():
        raise FileExistsError(
            f"Refusing to overwrite existing database: {destination_path}"
        )

    destination_path.parent.mkdir(parents=True, exist_ok=True)

    src = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(destination_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    return destination_path
