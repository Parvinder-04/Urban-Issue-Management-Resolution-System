"""Real-data-only application bootstrap.

Creates only the system service user. It never seeds departments, citizens,
complaints or demo records.

Schema creation and migration are owned by Alembic (see ``app.db_migrate``).
This module no longer calls ``Base.metadata.create_all()``: bootstrap runs only
after the startup guard has accepted an already-versioned schema.

Department metadata must come from a verified, provenance-preserving source.
When no verified department exists for a category, the application leaves the
issue unassigned and analytics report it as unassigned instead of inventing a
department name, email address or phone number.
"""
from uuid import uuid4
from app.database import SessionLocal, engine  # noqa: F401 - engine kept patchable
from app import models

SYSTEM_USER_EMAIL = "system@uimrs.local"


def ensure_bootstrap() -> None:
    db = SessionLocal()
    try:
        system_user = db.query(models.User).filter(
            models.User.email == SYSTEM_USER_EMAIL
        ).first()
        if not system_user:
            db.add(models.User(
                id=str(uuid4()), name="UIMRS System", email=SYSTEM_USER_EMAIL,
                password_hash="system-managed", role=models.UserRole.ADMIN,
            ))
        db.commit()
    finally:
        db.close()
