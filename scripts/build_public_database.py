"""Build or validate the public UIMRS SQLite database for deployment.

This script never copies a protected operational database and never writes to
one. It creates a *new* database from the Alembic revision chain, then seeds:

* the verified Gurugram department directory from
  ``deploy/seed/verified_departments.json`` (provenance preserved);
* optionally one administrator account from environment variables.

Calling it repeatedly is safe: existing departments and users are left
untouched, and an existing database is migrated (never recreated). No
historical Bengaluru rows and no synthetic evaluation dataset are copied.

Environment variables used for the optional administrator seed:

``UIMRS_SEED_ADMIN_EMAIL``
    Administrator e-mail. Must be a valid, unique address.
``UIMRS_SEED_ADMIN_PASSWORD``
    Initial password, at least 12 characters. Only read from the environment so
    it never appears in process arguments or logs.
``UIMRS_SEED_ADMIN_NAME``
    Optional display name (default: ``UIMRS Administrator``).

The password is hashed with the application's existing bcrypt helper. An
existing account with the same e-mail is never modified; if it is not an
administrator the script fails instead of escalating its role.

Usage::

    DATABASE_URL=sqlite:////var/data/uimrs_public.db \
    UIMRS_ENV=production \
    SECRET_KEY=... \
    UIMRS_SEED_ADMIN_EMAIL=admin@example.org \
    UIMRS_SEED_ADMIN_PASSWORD=... \
    python scripts/build_public_database.py

The destination must contain ``public`` in its filename and must not be one of
the protected operational databases.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

DEFAULT_SEED_FILE = PROJECT_ROOT / "deploy" / "seed" / "verified_departments.json"
DEFAULT_DESTINATION = PROJECT_ROOT / "data" / "public" / "uimrs_public.db"

PROTECTED_DATABASE_NAMES = {
    "uimrs.db",
    "uimrs_india_dev.db",
    "uimrs_india_candidate.db",
}
PROTECTED_BACKUP_PREFIXES = ("uimrs.db.backup", "uimrs_india_candidate")
MIN_ADMIN_PASSWORD_LENGTH = 12


class PublicDatabaseError(RuntimeError):
    """Raised when the public database cannot be built safely."""


def _destination_from_env() -> Path:
    explicit = (os.environ.get("UIMRS_PUBLIC_DB_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    prefix = "sqlite:///"
    if database_url.startswith(prefix):
        raw = database_url[len(prefix):]
        if raw and raw != ":memory:":
            return Path(raw)
    return DEFAULT_DESTINATION


def validate_destination(path: Path) -> Path:
    """Reject protected databases and any path that is not clearly public."""
    resolved = path.expanduser().resolve()
    name = resolved.name.lower()

    if resolved.suffix.lower() != ".db":
        raise PublicDatabaseError(
            "The public database destination must use a '.db' filename."
        )
    if name in PROTECTED_DATABASE_NAMES:
        raise PublicDatabaseError(
            f"Refusing to write to protected database '{resolved.name}'."
        )
    if name.startswith(PROTECTED_BACKUP_PREFIXES):
        raise PublicDatabaseError(
            f"Refusing to write to protected database backup '{resolved.name}'."
        )
    if "test_environment" in {part.lower() for part in resolved.parts}:
        raise PublicDatabaseError(
            "Refusing to write inside the isolated test_environment directory."
        )
    if "backups" in {part.lower() for part in resolved.parts}:
        raise PublicDatabaseError(
            "Refusing to write inside a backups directory."
        )
    if "public" not in name:
        raise PublicDatabaseError(
            "The destination filename must contain 'public' "
            "(for example uimrs_public.db) so an operational database can "
            "never be selected by mistake."
        )
    return resolved


def _category(value: str):
    from app import models

    cleaned = (value or "").strip()
    try:
        return models.IssueCategory[cleaned]
    except KeyError:
        return models.IssueCategory(cleaned)


def load_seed_file(path: Path) -> dict:
    if not path.is_file():
        raise PublicDatabaseError(f"Seed file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PublicDatabaseError(f"Seed file is not valid JSON: {path}: {exc}") from exc

    departments = payload.get("departments")
    if not isinstance(departments, list) or not departments:
        raise PublicDatabaseError("Seed file contains no departments.")
    return payload


def seed_departments(db, payload: dict) -> tuple[int, int]:
    """Insert missing verified departments. Returns (inserted, skipped)."""
    from app import models

    inserted = 0
    skipped = 0
    seen = set()

    for item in payload["departments"]:
        category = _category(item.get("handles_category", ""))
        city = item.get("city")
        name = item.get("name")
        if not name or not city:
            raise PublicDatabaseError("Every seed department needs a name and city.")

        key = (name, category, city)
        if key in seen:
            skipped += 1
            continue
        seen.add(key)

        existing = (
            db.query(models.Department)
            .filter(
                models.Department.name == name,
                models.Department.handles_category == category,
                models.Department.city == city,
            )
            .first()
        )
        if existing is not None:
            skipped += 1
            continue

        department_kwargs = dict(
            name=name,
            handles_category=category,
            contact_email=item.get("contact_email"),
            contact_phone=item.get("contact_phone"),
            city=city,
            source_agency=item.get("source_agency"),
            short_name=item.get("short_name"),
            jurisdiction=item.get("jurisdiction"),
            contact_purpose=item.get("contact_purpose"),
            verification_source_url=item.get("verification_source_url"),
            verification_date=item.get("verification_date"),
            routing_mode=item.get("routing_mode") or "manual_review",
            is_active=bool(item.get("is_active", True)),
        )
        if item.get("id"):
            department_kwargs["id"] = item["id"]

        department = models.Department(**department_kwargs)
        db.add(department)
        inserted += 1

    db.commit()
    return inserted, skipped


def seed_admin(db, *, email: str | None, password: str | None, name: str | None) -> str:
    """Create the first administrator without ever resetting an existing one."""
    from app import models
    from app.routers.auth import hash_password

    email = (email or "").strip().lower()
    password = password or ""

    if not email and not password:
        return "not configured"
    if not email or not password:
        raise PublicDatabaseError(
            "Set both UIMRS_SEED_ADMIN_EMAIL and UIMRS_SEED_ADMIN_PASSWORD, "
            "or neither."
        )
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise PublicDatabaseError("UIMRS_SEED_ADMIN_EMAIL is not a valid e-mail address.")
    if len(password) < MIN_ADMIN_PASSWORD_LENGTH:
        raise PublicDatabaseError(
            f"UIMRS_SEED_ADMIN_PASSWORD must be at least "
            f"{MIN_ADMIN_PASSWORD_LENGTH} characters."
        )

    existing = (
        db.query(models.User)
        .filter(models.User.email == email)
        .first()
    )
    if existing is not None:
        if existing.role != models.UserRole.ADMIN:
            raise PublicDatabaseError(
                "A non-administrator account already uses the configured seed "
                "e-mail. Refusing to change its role."
            )
        return "already present"

    db.add(
        models.User(
            name=(name or "").strip() or "UIMRS Administrator",
            email=email,
            password_hash=hash_password(password),
            role=models.UserRole.ADMIN,
            preferred_language="en",
        )
    )
    db.commit()
    return "created"


def build_public_database(
    destination=None,
    *,
    database_url: str | None = None,
    seed_file: Path = DEFAULT_SEED_FILE,
    admin_email: str | None = None,
    admin_password: str | None = None,
    admin_name: str | None = None,
) -> dict:
    """Create or update the public database. Returns a summary mapping.

    ``destination`` is used for SQLite databases. ``database_url`` overrides it
    and supports any SQLAlchemy URL, including PostgreSQL.
    """
    if database_url:
        url = database_url.strip()
    else:
        if destination is None:
            destination = _destination_from_env()
        destination = validate_destination(Path(destination))
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{destination.as_posix()}"

    if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
        sqlite_path = Path(url[len("sqlite:///"):])
        if sqlite_path.name:
            validate_destination(sqlite_path)
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    # Configuration is captured when the app modules are first imported, so the
    # URL must be fixed before any of them are loaded.
    os.environ["DATABASE_URL"] = url

    from app import models
    from app.bootstrap import ensure_bootstrap
    from app.database import SessionLocal, engine
    from app.db_guard import guard_startup
    from app.db_migrate import HEAD_REVISION, current_revision, prepare_database

    payload = load_seed_file(seed_file)
    city = payload.get("city") or ""
    if "Gurugram" not in city:
        raise PublicDatabaseError(
            "The verified seed file must describe the Gurugram directory."
        )

    migration = prepare_database(url)
    state = guard_startup(engine, url)
    if state != "validated":
        raise PublicDatabaseError(
            f"Database schema did not validate after migration (state={state})."
        )
    if current_revision(url) != HEAD_REVISION:
        raise PublicDatabaseError("Database is not at the expected Alembic head.")
    ensure_bootstrap()

    db = SessionLocal()
    try:
        inserted, skipped = seed_departments(db, payload)
        admin_state = seed_admin(
            db,
            email=admin_email,
            password=admin_password,
            name=admin_name,
        )
        counts = {
            "departments": db.query(models.Department).count(),
            "users": db.query(models.User).count(),
            "issues": db.query(models.Issue).count(),
        }
    finally:
        db.close()
        engine.dispose()

    return {
        "destination": url,
        "migration_state": migration.state,
        "revision": current_revision(url),
        "departments_inserted": inserted,
        "departments_skipped": skipped,
        "admin": admin_state,
        "counts": counts,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create or validate a public UIMRS SQLite database without copying "
            "any protected operational database."
        )
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy database URL (SQLite or PostgreSQL). Overrides --destination.",
    )
    parser.add_argument(
        "--destination",
        default=str(_destination_from_env()),
        help=(
            "Destination SQLite file. Defaults to DATABASE_URL when it is a "
            "SQLite URL, else data/public/uimrs_public.db."
        ),
    )
    parser.add_argument(
        "--seed-file",
        default=str(DEFAULT_SEED_FILE),
        help="Verified department seed JSON.",
    )
    parser.add_argument(
        "--admin-email",
        default=os.environ.get("UIMRS_SEED_ADMIN_EMAIL"),
        help="Optional administrator e-mail (or UIMRS_SEED_ADMIN_EMAIL).",
    )
    parser.add_argument(
        "--admin-name",
        default=os.environ.get("UIMRS_SEED_ADMIN_NAME"),
        help="Optional administrator display name.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = build_public_database(
            destination=args.destination if not args.database_url else None,
            database_url=args.database_url,
            seed_file=Path(args.seed_file),
            admin_email=args.admin_email,
            admin_password=os.environ.get("UIMRS_SEED_ADMIN_PASSWORD"),
            admin_name=args.admin_name,
        )
    except PublicDatabaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print("Public UIMRS database ready.")
    print(f"  destination:        {summary['destination']}")
    print(f"  migration state:    {summary['migration_state']}")
    print(f"  schema revision:    {summary['revision']}")
    print(f"  departments added:  {summary['departments_inserted']}")
    print(f"  departments kept:   {summary['departments_skipped']}")
    print(f"  administrator seed: {summary['admin']}")
    print(
        "  row counts:         "
        f"{summary['counts']['departments']} departments, "
        f"{summary['counts']['users']} users, "
        f"{summary['counts']['issues']} issues"
    )
    print("No historical Bengaluru rows and no synthetic dataset were copied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
