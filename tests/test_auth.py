"""
Comprehensive Authentication & Security Foundation Test Suite for UIMRS.

Runs against an ISOLATED in-memory SQLite database (sqlite:///:memory:).
Never modifies or interacts with candidate or production databases.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure backend directory is in python search path
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.core.config import get_settings
from app.routers.auth import (
    BCRYPT_MAX_BYTES,
    create_access_token,
    get_current_user,
    get_profile,
    hash_password,
    login,
    register,
    require_role,
    verify_password,
)

settings = get_settings()


class BaseAuthTestCase(unittest.TestCase):
    """Sets up an isolated in-memory SQLite database for each test."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
        )
        models.Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        models.Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()


class TestPasswordSecurity(BaseAuthTestCase):
    """Tests password hashing, verification, length limits, and edge cases."""

    def test_hash_and_verify_valid_password(self):
        plain = "CorrectPassword#2026"
        hashed = hash_password(plain)
        self.assertTrue(hashed.startswith("$2b$"))
        self.assertNotEqual(plain, hashed)
        self.assertTrue(verify_password(plain, hashed))

    def test_verify_wrong_password_returns_false(self):
        hashed = hash_password("Secret12345")
        self.assertFalse(verify_password("WrongPassword12345", hashed))

    def test_verify_empty_or_none_safely_returns_false(self):
        hashed = hash_password("Secret12345")
        self.assertFalse(verify_password("", hashed))
        self.assertFalse(verify_password(None, hashed))
        self.assertFalse(verify_password("Secret12345", ""))
        self.assertFalse(verify_password("Secret12345", None))

    def test_hash_password_exceeding_72_bytes_raises_error(self):
        long_password = "a" * (BCRYPT_MAX_BYTES + 1)
        with self.assertRaises(ValueError) as ctx:
            hash_password(long_password)
        self.assertIn("cannot exceed 72 bytes", str(ctx.exception))

    def test_verify_password_exceeding_72_bytes_fails_safely(self):
        hashed = hash_password("ValidPassword123")
        long_password = "x" * 100
        # Must fail safely without raising ValueError / crashing
        result = verify_password(long_password, hashed)
        self.assertFalse(result)

    def test_verify_malformed_hash_fails_safely(self):
        # Legacy placeholder strings or invalid formats must return False
        self.assertFalse(verify_password("any_pass", "system-managed"))
        self.assertFalse(verify_password("any_pass", "import-only"))
        self.assertFalse(verify_password("any_pass", "not-a-valid-bcrypt-hash"))

    def test_schema_password_length_enforcement(self):
        # Short password (<8)
        with self.assertRaises(ValidationError):
            schemas.UserCreate(
                name="Short User",
                email="short@uimrs.org",
                password="short",
            )
        # Overly long password (>72 bytes)
        with self.assertRaises(ValidationError):
            schemas.UserCreate(
                name="Long User",
                email="long@uimrs.org",
                password="a" * 80,
            )
        # Valid password
        valid_user = schemas.UserCreate(
            name="Valid User",
            email="valid@uimrs.org",
            password="ValidPassword#123",
        )
        self.assertEqual(valid_user.password, "ValidPassword#123")


class TestUserRegistration(BaseAuthTestCase):
    """Tests citizen registration flow and validation."""

    def test_register_citizen_success(self):
        payload = schemas.UserCreate(
            name="Test Citizen",
            email="test.citizen@example.com",
            phone="9876543210",
            password="SecurePassword@123",
            preferred_language="hi",
        )
        user = register(payload, db=self.db)

        self.assertIsNotNone(user.id)
        self.assertEqual(user.name, "Test Citizen")
        self.assertEqual(user.email, "test.citizen@example.com")
        self.assertEqual(user.role, models.UserRole.CITIZEN)
        self.assertEqual(user.preferred_language, "hi")
        # Ensure password was hashed with native bcrypt
        self.assertTrue(user.password_hash.startswith("$2b$"))
        self.assertNotEqual(user.password_hash, "SecurePassword@123")
        self.assertTrue(verify_password("SecurePassword@123", user.password_hash))

    def test_register_duplicate_email_rejected(self):
        payload = schemas.UserCreate(
            name="User One",
            email="duplicate@example.com",
            password="PasswordOne123",
        )
        register(payload, db=self.db)

        # Attempt to register with the same email
        payload_dup = schemas.UserCreate(
            name="User Two",
            email="duplicate@example.com",
            password="PasswordTwo123",
        )
        with self.assertRaises(HTTPException) as ctx:
            register(payload_dup, db=self.db)
        self.assertEqual(ctx.exception.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(ctx.exception.detail, "Email already registered")

    def test_register_password_not_exposed_in_schema(self):
        payload = schemas.UserCreate(
            name="Citizen Privacy",
            email="privacy@example.com",
            password="PrivatePassword123",
        )
        user = register(payload, db=self.db)
        user_out = schemas.UserOut.model_validate(user)
        user_dict = user_out.model_dump()

        self.assertNotIn("password", user_dict)
        self.assertNotIn("password_hash", user_dict)


class TestLoginAndToken(BaseAuthTestCase):
    """Tests JSON-based login and token issuance."""

    def setUp(self):
        super().setUp()
        # Seed a test citizen in the isolated in-memory DB
        self.test_password = "CitizenSecret@2026"
        self.citizen = models.User(
            name="Test Citizen",
            email="citizen@uimrs.org",
            password_hash=hash_password(self.test_password),
            role=models.UserRole.CITIZEN,
            preferred_language="en",
        )
        self.db.add(self.citizen)
        self.db.commit()
        self.db.refresh(self.citizen)

    def test_login_success_with_json_body(self):
        login_req = schemas.LoginRequest(
            email="citizen@uimrs.org",
            password=self.test_password,
        )
        res = login(login_req, db=self.db)

        self.assertIn("access_token", res)
        self.assertEqual(res["token_type"], "bearer")
        self.assertEqual(res["user"].email, "citizen@uimrs.org")
        self.assertEqual(res["user"].role, models.UserRole.CITIZEN)

        # Validate that the issued token is valid
        payload = jwt.decode(
            res["access_token"],
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
        self.assertEqual(payload["sub"], self.citizen.id)
        self.assertEqual(payload["email"], "citizen@uimrs.org")
        self.assertEqual(payload["role"], "citizen")

    def test_login_wrong_password_rejected(self):
        login_req = schemas.LoginRequest(
            email="citizen@uimrs.org",
            password="IncorrectPassword!",
        )
        with self.assertRaises(HTTPException) as ctx:
            login(login_req, db=self.db)
        self.assertEqual(ctx.exception.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(ctx.exception.detail, "Invalid credentials")

    def test_login_nonexistent_email_rejected(self):
        login_req = schemas.LoginRequest(
            email="nonexistent@uimrs.org",
            password="AnyPassword123",
        )
        with self.assertRaises(HTTPException) as ctx:
            login(login_req, db=self.db)
        self.assertEqual(ctx.exception.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(ctx.exception.detail, "Invalid credentials")


class TestJWTTokenValidation(BaseAuthTestCase):
    """Tests JWT creation, signature, expiration, and dependency extraction."""

    def setUp(self):
        super().setUp()
        self.user = models.User(
            name="JWT Test User",
            email="jwt.user@uimrs.org",
            password_hash=hash_password("Pass123456"),
            role=models.UserRole.CITIZEN,
        )
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)

    def test_get_current_user_valid_token(self):
        token = create_access_token(self.user)
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        authenticated_user = get_current_user(credentials=creds, db=self.db)
        self.assertEqual(authenticated_user.id, self.user.id)
        self.assertEqual(authenticated_user.email, self.user.email)

    def test_get_current_user_missing_credentials(self):
        with self.assertRaises(HTTPException) as ctx:
            get_current_user(credentials=None, db=self.db)
        self.assertEqual(ctx.exception.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(ctx.exception.detail, "Authentication required")

    def test_get_current_user_expired_token(self):
        expired_token = create_access_token(
            self.user,
            expires_delta=timedelta(seconds=-10),
        )
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=expired_token)
        with self.assertRaises(HTTPException) as ctx:
            get_current_user(credentials=creds, db=self.db)
        self.assertEqual(ctx.exception.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(ctx.exception.detail, "Token has expired")

    def test_get_current_user_tampered_signature(self):
        token = create_access_token(self.user)
        tampered_token = token[:-5] + "XXXXX"
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=tampered_token)
        with self.assertRaises(HTTPException) as ctx:
            get_current_user(credentials=creds, db=self.db)
        self.assertEqual(ctx.exception.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(ctx.exception.detail, "Could not validate credentials")

    def test_get_current_user_malformed_token(self):
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="not-a-jwt")
        with self.assertRaises(HTTPException) as ctx:
            get_current_user(credentials=creds, db=self.db)
        self.assertEqual(ctx.exception.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(ctx.exception.detail, "Could not validate credentials")

    def test_get_current_user_deleted_user(self):
        # Create token then delete user
        token = create_access_token(self.user)
        self.db.delete(self.user)
        self.db.commit()

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with self.assertRaises(HTTPException) as ctx:
            get_current_user(credentials=creds, db=self.db)
        self.assertEqual(ctx.exception.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(ctx.exception.detail, "User not found")


class TestRoleBasedAccessControl(BaseAuthTestCase):
    """Tests role checking across citizen, officer, and admin roles."""

    def setUp(self):
        super().setUp()
        self.citizen = models.User(
            name="Citizen A",
            email="citizen.a@uimrs.org",
            password_hash=hash_password("CitizenPass#1"),
            role=models.UserRole.CITIZEN,
        )
        self.officer = models.User(
            name="Officer B",
            email="officer.b@uimrs.org",
            password_hash=hash_password("OfficerPass#1"),
            role=models.UserRole.OFFICER,
        )
        self.admin = models.User(
            name="Admin C",
            email="admin.c@uimrs.org",
            password_hash=hash_password("AdminPass#1"),
            role=models.UserRole.ADMIN,
        )
        self.db.add_all([self.citizen, self.officer, self.admin])
        self.db.commit()

    def test_citizen_allowed_on_citizen_route(self):
        checker = require_role(models.UserRole.CITIZEN)
        result = checker(current_user=self.citizen)
        self.assertEqual(result.id, self.citizen.id)

    def test_citizen_denied_on_officer_route(self):
        checker = require_role(models.UserRole.OFFICER)
        with self.assertRaises(HTTPException) as ctx:
            checker(current_user=self.citizen)
        self.assertEqual(ctx.exception.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Required role: officer", ctx.exception.detail)

    def test_citizen_denied_on_admin_route(self):
        checker = require_role(models.UserRole.ADMIN)
        with self.assertRaises(HTTPException) as ctx:
            checker(current_user=self.citizen)
        self.assertEqual(ctx.exception.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Required role: admin", ctx.exception.detail)

    def test_officer_allowed_on_officer_route(self):
        checker = require_role(models.UserRole.OFFICER)
        result = checker(current_user=self.officer)
        self.assertEqual(result.id, self.officer.id)

    def test_officer_denied_on_admin_route(self):
        checker = require_role(models.UserRole.ADMIN)
        with self.assertRaises(HTTPException) as ctx:
            checker(current_user=self.officer)
        self.assertEqual(ctx.exception.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Required role: admin", ctx.exception.detail)

    def test_admin_allowed_on_admin_route(self):
        checker = require_role(models.UserRole.ADMIN)
        result = checker(current_user=self.admin)
        self.assertEqual(result.id, self.admin.id)

    def test_multi_role_allowance(self):
        # Route allowing both officer and admin
        checker = require_role([models.UserRole.OFFICER, models.UserRole.ADMIN])
        self.assertEqual(checker(current_user=self.officer).id, self.officer.id)
        self.assertEqual(checker(current_user=self.admin).id, self.admin.id)

        # Citizen must be rejected
        with self.assertRaises(HTTPException) as ctx:
            checker(current_user=self.citizen)
        self.assertEqual(ctx.exception.status_code, status.HTTP_403_FORBIDDEN)

    def test_get_profile_endpoint(self):
        profile = get_profile(current_user=self.citizen)
        self.assertEqual(profile.email, self.citizen.email)


if __name__ == "__main__":
    unittest.main()
