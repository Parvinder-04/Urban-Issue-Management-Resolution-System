"""Provision one public UIMRS administrator account.

There is no public administrator registration. An operator with local access to
the deployment database runs this utility once (or again for another
administrator).

Usage (from the project root, after migrations and department seeding):

    backend/venv/Scripts/python.exe scripts/provision_public_admin.py `
        --name "Your Name" --email "you@example.com"

The password is requested twice through hidden prompts. It is never accepted on
the command line, never printed and never stored in plaintext. The ADMIN role is
forced in code; the caller cannot choose a role. An explicit confirmation is
required before the account is created.

``--database-url`` defaults to the application ``DATABASE_URL``. This utility
never modifies an existing user.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from pydantic import ValidationError  # noqa: E402
from sqlalchemy import create_engine, func  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app import models, schemas  # noqa: E402
from app.core.config import resolve_database_url  # noqa: E402
from app.db_guard import guard_startup  # noqa: E402
from app.db_migrate import HEAD_REVISION, current_revision  # noqa: E402
from app.routers.auth import hash_password  # noqa: E402

CONFIRMATION = "CREATE PUBLIC ADMIN"


class AdminProvisioningError(ValueError):
    """A controlled, user-facing provisioning refusal."""


@dataclass(frozen=True)
class ProvisionedAdmin:
    id: str
    name: str
    email: str
    role: str
    created_at: object | None


def _validation_message(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = error.get("msg", "Invalid value")
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts) or "Invalid administrator details."


def provision_admin(
    db: Session,
    *,
    name: str,
    email: str,
    password: str,
    phone: str | None = None,
    preferred_language: str = "en",
    commit: bool = True,
) -> ProvisionedAdmin:
    """Create one ADMIN account. The role is forced and duplicates are refused."""
    try:
        validated = schemas.UserCreate(
            name=name,
            email=email,
            phone=phone,
            password=password,
            preferred_language=preferred_language,
        )
    except ValidationError as exc:
        raise AdminProvisioningError(_validation_message(exc)) from exc

    clean_name = validated.name.strip()
    if not clean_name:
        raise AdminProvisioningError("Administrator name is required.")

    clean_email = str(validated.email).strip()
    clean_phone = validated.phone.strip() if validated.phone else None

    duplicate = (
        db.query(models.User)
        .filter(func.lower(models.User.email) == clean_email.lower())
        .first()
    )
    if duplicate is not None:
        raise AdminProvisioningError(
            "An account with this e-mail address already exists. Existing users "
            "are never modified by this utility."
        )
    if clean_phone:
        phone_duplicate = (
            db.query(models.User)
            .filter(models.User.phone == clean_phone)
            .first()
        )
        if phone_duplicate is not None:
            raise AdminProvisioningError(
                "An account with this phone number already exists."
            )

    try:
        password_hash = hash_password(validated.password)
    except ValueError as exc:
        raise AdminProvisioningError(str(exc)) from exc

    user = models.User(
        name=clean_name,
        email=clean_email,
        phone=clean_phone,
        password_hash=password_hash,
        role=models.UserRole.ADMIN,
        department_id=None,
        preferred_language=validated.preferred_language,
    )
    db.add(user)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
        db.refresh(user)
    except IntegrityError as exc:
        db.rollback()
        raise AdminProvisioningError(
            "Could not create the administrator account because a unique field "
            "(e-mail or phone) is already in use."
        ) from exc

    return ProvisionedAdmin(
        id=user.id,
        name=user.name,
        email=user.email,
        role=user.role.value,
        created_at=user.created_at,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create one ADMIN account in the public UIMRS database. The "
            "password is requested through hidden prompts."
        )
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy URL. Defaults to the application DATABASE_URL.",
    )
    parser.add_argument("--name", required=True, help="Administrator full name.")
    parser.add_argument("--email", required=True, help="Administrator e-mail.")
    parser.add_argument("--phone", default=None, help="Optional phone number.")
    return parser


def _make_engine(database_url: str):
    kwargs = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(database_url, **kwargs)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database_url = args.database_url or resolve_database_url()

    engine = _make_engine(database_url)
    try:
        state = guard_startup(engine, database_url)
        if state != "validated":
            print(
                "The target database has no validated UIMRS schema. Apply the "
                "Alembic migrations first.",
                file=sys.stderr,
            )
            return 1
        revision = current_revision(database_url)
        if revision != HEAD_REVISION:
            print(
                f"Database revision {revision!r} does not match {HEAD_REVISION!r}.",
                file=sys.stderr,
            )
            return 1

        print(f"Database: {database_url}")
        print(f"Account to create: {args.email} (role ADMIN)")
        print("The password is requested through hidden prompts.")
        confirmation = input(f"Type '{CONFIRMATION}' to confirm: ").strip()
        if confirmation != CONFIRMATION:
            print("Not confirmed. No account was created.", file=sys.stderr)
            return 1

        password = getpass.getpass("Administrator password (hidden): ")
        confirmation_password = getpass.getpass("Confirm password (hidden): ")
        if password != confirmation_password:
            print("Passwords do not match. No account was created.", file=sys.stderr)
            return 1

        session_factory = sessionmaker(
            autocommit=False, autoflush=False, bind=engine
        )
        db = session_factory()
        try:
            provisioned = provision_admin(
                db,
                name=args.name,
                email=args.email,
                phone=args.phone,
                password=password,
            )
        except AdminProvisioningError as exc:
            print(f"Could not create administrator: {exc}", file=sys.stderr)
            return 1
        finally:
            db.close()
    finally:
        engine.dispose()

    print()
    print("Administrator created successfully.")
    print(f"  id:     {provisioned.id}")
    print(f"  name:   {provisioned.name}")
    print(f"  e-mail: {provisioned.email}")
    print(f"  role:   {provisioned.role}")
    print("The password was hashed and stored. It is never displayed.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
