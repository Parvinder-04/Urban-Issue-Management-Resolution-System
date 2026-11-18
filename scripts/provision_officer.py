"""Interactively provision one municipal officer account.

Usage (from the project root):

    backend/venv/Scripts/python.exe scripts/provision_officer.py

The utility:
  * lists real, active departments in supported cities (it never creates one);
  * asks for the officer name, e-mail and optional official phone;
  * reads the initial password through a hidden terminal prompt (twice);
  * forces the role to OFFICER and hashes the password with the existing
    bcrypt helper;
  * rejects duplicate e-mail addresses and never modifies an existing user;
  * never accepts a password on the command line and never prints the password
    or its hash.

An authorised administrator should run this against the intended database.
``--database-url`` is available for controlled/maintenance use; the default is
the application ``DATABASE_URL`` (the documented development working copy when
unset).
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.config import resolve_database_url  # noqa: E402
from app.db_guard import guard_startup  # noqa: E402
from app.services.officer_provisioning import (  # noqa: E402
    OfficerProvisioningError,
    list_provisionable_departments,
    provision_officer,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser. There is deliberately no command-line password option."""
    parser = argparse.ArgumentParser(
        description=(
            "Create one OFFICER account in an existing active, supported-city "
            "department. The password is requested through a hidden prompt."
        )
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help=(
            "SQLAlchemy database URL. Defaults to the application DATABASE_URL "
            "or the documented development database."
        ),
    )
    return parser


def _make_engine(database_url: str):
    kwargs = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(database_url, **kwargs)


def _prompt_required(label: str) -> str:
    while True:
        value = input(label).strip()
        if value:
            return value
        print("A value is required.")


def _prompt_optional(label: str) -> str | None:
    value = input(label).strip()
    return value or None


def _department_label(index: int, department) -> str:
    category = (
        department.handles_category.value
        if getattr(department.handles_category, "value", None)
        else department.handles_category
    )
    return (
        f"{index}. {department.name} | {department.city} | "
        f"{category} | routing={department.routing_mode or 'unknown'}"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database_url = args.database_url or resolve_database_url()

    engine = _make_engine(database_url)
    try:
        state = guard_startup(engine, database_url)
    except Exception as exc:
        print(f"Database validation failed: {exc}", file=sys.stderr)
        return 1

    if state != "validated":
        print(
            "The target database has no UIMRS schema yet. Apply the reviewed "
            "Alembic migration before provisioning officers.",
            file=sys.stderr,
        )
        return 1

    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = session_factory()
    try:
        departments = list_provisionable_departments(db)
        if not departments:
            print(
                "No active departments exist in a supported city. "
                "Provisioning never creates departments.",
                file=sys.stderr,
            )
            return 1

        print("Existing active departments in supported cities:")
        for index, department in enumerate(departments, start=1):
            print(_department_label(index, department))

        choice = input("Select department number: ").strip()
        if not choice.isdigit() or not (1 <= int(choice) <= len(departments)):
            print("Invalid department selection.", file=sys.stderr)
            return 1
        department = departments[int(choice) - 1]

        print()
        name = _prompt_required("Officer full name: ")
        email = _prompt_required("Officer e-mail: ")
        phone = _prompt_optional("Official phone (optional, press Enter to skip): ")

        password = getpass.getpass("Initial password (hidden): ")
        confirmation = getpass.getpass("Confirm password (hidden): ")
        if password != confirmation:
            print("Passwords do not match. No account was created.", file=sys.stderr)
            return 1

        try:
            provisioned = provision_officer(
                db,
                name=name,
                email=email,
                phone=phone,
                department_id=department.id,
                password=password,
            )
        except OfficerProvisioningError as exc:
            print(f"Could not create officer: {exc}", file=sys.stderr)
            return 1

        print()
        print("Officer account created successfully.")
        print(f"  id:         {provisioned.id}")
        print(f"  name:       {provisioned.name}")
        print(f"  e-mail:     {provisioned.email}")
        print(f"  role:       {provisioned.role}")
        print(f"  department: {provisioned.department_name} ({provisioned.department_city})")
        print("The password was hashed and stored. It is never displayed.")
        return 0
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover - exercised through tests/subprocess
    raise SystemExit(main())
