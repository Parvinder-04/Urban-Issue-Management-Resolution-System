"""UIMRS Task 11B — system-managed administrator account security."""
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.bootstrap import SYSTEM_USER_EMAIL, ensure_bootstrap
from app.database import Base
from app.routers.auth import login, verify_password


class TestSystemUserSecurity(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def _system_user(self):
        user = models.User(
            name="UIMRS System",
            email=SYSTEM_USER_EMAIL,
            password_hash="system-managed",
            role=models.UserRole.ADMIN,
        )
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user

    def test_system_password_hash_is_not_a_valid_credential(self):
        self.assertFalse(verify_password("system-managed", "system-managed"))
        self.assertFalse(verify_password("", "system-managed"))
        self.assertFalse(verify_password("anything", "system-managed"))

    def test_system_user_cannot_log_in_with_its_stored_hash(self):
        self._system_user()
        # The system account uses a non-public email domain, so the normal
        # login request validation rejects it before authentication.
        with self.assertRaises(ValidationError):
            schemas.LoginRequest(email=SYSTEM_USER_EMAIL, password="system-managed")

    def test_system_user_cannot_log_in_with_empty_password(self):
        self._system_user()
        with self.assertRaises(ValidationError):
            login(schemas.LoginRequest(email=SYSTEM_USER_EMAIL, password=""), self.db)

    def test_bootstrap_preserves_system_user_shape(self):
        # ensure_bootstrap uses the configured engine/session. Exercise the
        # bootstrap helper against an isolated in-memory database by patching
        # the module-level session factory.
        from unittest.mock import patch

        with patch("app.bootstrap.SessionLocal", self.Session):
            ensure_bootstrap()
        session = self.Session()
        try:
            user = (
                session.query(models.User)
                .filter(models.User.email == SYSTEM_USER_EMAIL)
                .first()
            )
            self.assertIsNotNone(user)
            self.assertEqual(user.role, models.UserRole.ADMIN)
            self.assertEqual(user.password_hash, "system-managed")
            self.assertFalse(verify_password("system-managed", user.password_hash))
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
