"""Controlled officer-account provisioning.

Public registration always creates citizens. Officer accounts are created only
through this service / the interactive ``scripts/provision_officer.py`` utility.

Security properties:
* the role is forced to ``OFFICER`` — no caller-supplied role is accepted;
* the department must already exist, be active and belong to a supported city;
* the password is validated with the same schema rules as registration and
  hashed with the existing bcrypt helper;
* duplicate e-mail addresses are rejected instead of modifying an existing user;
* passwords and password hashes are never returned by the CLI or stored in
  diagnostic messages.
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models, schemas
from app.routers.auth import hash_password
from app.services.officer_workflow import SUPPORTED_OFFICER_CITIES


class OfficerProvisioningError(ValueError):
    """A controlled, user-facing provisioning refusal."""


@dataclass(frozen=True)
class ProvisionedOfficer:
    id: str
    name: str
    email: str
    phone: str | None
    role: str
    department_id: str
    department_name: str
    department_city: str


def list_provisionable_departments(db: Session) -> list[models.Department]:
    """Return existing active departments in supported cities.

    No department is ever created by this module. The list is used by the
    interactive utility so the administrator selects a real directory row.
    """
    return (
        db.query(models.Department)
        .filter(
            models.Department.is_active.is_(True),
            models.Department.city.in_(SUPPORTED_OFFICER_CITIES),
        )
        .order_by(
            models.Department.city.asc(),
            models.Department.name.asc(),
            models.Department.handles_category.asc(),
        )
        .all()
    )


def _validation_message(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = error.get("msg", "Invalid value")
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts) or "Invalid officer details."


def provision_officer(
    db: Session,
    *,
    name: str,
    email: str,
    password: str,
    department_id: str,
    phone: str | None = None,
    preferred_language: str = "en",
    commit: bool = True,
) -> ProvisionedOfficer:
    """Create one verified OFFICER account in an existing department.

    The caller must supply a real department id. The function never creates a
    department, never changes an existing user and never accepts a role.

    ``commit=False`` lets an administrative caller add an audit event to the
    same transaction. The account is flushed (so unique constraints and its id
    are available) but not committed; the caller must commit or roll back.
    """
    # Reuse the public registration contract for name/e-mail/password rules.
    try:
        validated = schemas.UserCreate(
            name=name,
            email=email,
            phone=phone,
            password=password,
            preferred_language=preferred_language,
        )
    except ValidationError as exc:
        raise OfficerProvisioningError(_validation_message(exc)) from exc

    clean_name = validated.name.strip()
    if not clean_name:
        raise OfficerProvisioningError("Officer name is required.")

    clean_email = str(validated.email).strip()
    clean_phone = validated.phone.strip() if validated.phone else None

    department = (
        db.query(models.Department)
        .filter(models.Department.id == department_id)
        .first()
    )
    if department is None:
        raise OfficerProvisioningError(
            "Selected department does not exist. Choose an existing verified department."
        )
    if not department.is_active:
        raise OfficerProvisioningError(
            "Selected department is inactive and cannot receive an officer account."
        )
    if department.city not in SUPPORTED_OFFICER_CITIES:
        raise OfficerProvisioningError(
            "Selected department does not belong to a supported operating city."
        )

    duplicate = (
        db.query(models.User)
        .filter(func.lower(models.User.email) == clean_email.lower())
        .first()
    )
    if duplicate is not None:
        raise OfficerProvisioningError(
            "An account with this e-mail address already exists. "
            "Existing users are never modified by this utility."
        )

    if clean_phone:
        phone_duplicate = (
            db.query(models.User)
            .filter(models.User.phone == clean_phone)
            .first()
        )
        if phone_duplicate is not None:
            raise OfficerProvisioningError(
                "An account with this official phone number already exists."
            )

    try:
        password_hash = hash_password(validated.password)
    except ValueError as exc:
        # The message never contains the password itself.
        raise OfficerProvisioningError(str(exc)) from exc

    user = models.User(
        name=clean_name,
        email=clean_email,
        phone=clean_phone,
        password_hash=password_hash,
        role=models.UserRole.OFFICER,
        department_id=department.id,
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
        raise OfficerProvisioningError(
            "Could not create the officer account because a unique field "
            "(e-mail or phone) is already in use."
        ) from exc

    return ProvisionedOfficer(
        id=user.id,
        name=user.name,
        email=user.email,
        phone=user.phone,
        role=user.role.value,
        department_id=department.id,
        department_name=department.name,
        department_city=department.city,
    )
