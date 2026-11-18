"""Validate the public UIMRS environment before the server starts.

This module is invoked by ``run_public_environment.ps1`` and can also be run
directly:

    backend/venv/Scripts/python.exe scripts/verify_public_startup.py

It reads ``DATABASE_URL`` and ``UPLOAD_DIR`` from the process environment (or
the optional command-line arguments), verifies SQLite integrity and the Alembic
revision **without applying migrations**, and confirms that the evidence
directory exists.

It never prints ``SECRET_KEY``. It does not restrict the database to a single
filename: an operator may deliberately point at an authorised historical
database. The application startup guard still enforces the full schema.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine  # noqa: E402

from app.core.config import resolve_database_url  # noqa: E402
from app.db_guard import guard_startup, sqlite_path_from_url  # noqa: E402
from app.db_migrate import HEAD_REVISION, current_revision  # noqa: E402


class StartupValidationError(RuntimeError):
    """A controlled, user-facing startup refusal."""


def _resolved_database_path(database_url: str) -> Path:
    selected = sqlite_path_from_url(database_url)
    if selected is None:
        raise StartupValidationError(
            "DATABASE_URL must be a file-backed SQLite URL, not an in-memory "
            "or non-SQLite URL."
        )
    return Path(selected).expanduser().resolve()


def validate_environment(
    environ=None,
    *,
    database_url: str | None = None,
    upload_dir: str | None = None,
) -> dict:
    """Validate the public environment and return a summary for the operator."""
    env = os.environ if environ is None else environ

    configured_url = (
        database_url or (env.get("DATABASE_URL") or "").strip()
    )
    if not configured_url:
        # The application has a documented local default; use it only when the
        # caller explicitly asks for validation without an environment.
        configured_url = resolve_database_url(env)

    selected_db = _resolved_database_path(configured_url)
    if not selected_db.is_file():
        raise StartupValidationError(
            f"Database not found: {selected_db}. Run the public setup script or "
            "scripts/migrate_database.py first."
        )

    uploads_value = upload_dir or (env.get("UPLOAD_DIR") or "").strip()
    if not uploads_value:
        raise StartupValidationError(
            "UPLOAD_DIR is not set. The public launcher must select an explicit "
            "evidence directory (default: local_data/uploads)."
        )
    uploads_path = Path(uploads_value).expanduser().resolve()
    if not uploads_path.is_dir():
        raise StartupValidationError(
            f"Evidence directory not found: {uploads_path}"
        )

    engine = create_engine(
        configured_url, connect_args={"check_same_thread": False}
    )
    try:
        state = guard_startup(engine, configured_url)
    finally:
        engine.dispose()

    if state != "validated":
        raise StartupValidationError(
            f"The database state is {state!r}; a validated UIMRS schema is "
            "required. No migrations were applied."
        )

    revision = current_revision(configured_url)
    if revision != HEAD_REVISION:
        raise StartupValidationError(
            f"Database revision {revision!r} does not match {HEAD_REVISION!r}. "
            "No migrations were applied."
        )

    return {
        "database_path": selected_db,
        "database_state": state,
        "database_revision": revision,
        "uploads_path": uploads_path,
        "frontend_mode": (env.get("UIMRS_FRONTEND_MODE") or "").strip() or None,
        "host": (env.get("UIMRS_HOST") or "").strip() or None,
        "port": (env.get("UIMRS_PORT") or "").strip() or None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the public UIMRS database and evidence directory."
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override DATABASE_URL for this validation.",
    )
    parser.add_argument(
        "--upload-dir",
        default=None,
        help="Override UPLOAD_DIR for this validation.",
    )
    args = parser.parse_args(argv)

    try:
        summary = validate_environment(
            database_url=args.database_url, upload_dir=args.upload_dir
        )
    except StartupValidationError as exc:
        print(f"Startup validation failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive, shown to operator
        print(f"Startup validation failed unexpectedly: {exc}", file=sys.stderr)
        return 1

    print(f"  database:          {summary['database_path']}")
    print(f"  database state:    {summary['database_state']}")
    print(f"  database revision: {summary['database_revision']}")
    print(f"  evidence:          {summary['uploads_path']}")
    print(f"  frontend mode:     {summary['frontend_mode']}")
    print(f"  bind address:      {summary['host']}:{summary['port']}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the launcher
    raise SystemExit(main())
