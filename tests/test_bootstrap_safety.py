"""
UIMRS Task 3 — Department bootstrap safety.

Verifies that application bootstrap never inserts generic or fabricated
department names, contact emails or phone numbers, never touches existing
verified department rows, and keeps the system-user bootstrap behaviour.

Every test runs against an isolated in-memory SQLite database by patching the
engine/session used by ``app.bootstrap``. The real development database is never
opened.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.bootstrap import SYSTEM_USER_EMAIL, ensure_bootstrap
from app.database import Base


class TestBootstrapSafety(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        # Never let bootstrap reach the real development database.
        self._engine_patch = patch("app.bootstrap.engine", self.engine)
        self._session_patch = patch("app.bootstrap.SessionLocal", self.Session)
        self._engine_patch.start()
        self._session_patch.start()
        self.addCleanup(self._session_patch.stop)
        self.addCleanup(self._engine_patch.stop)

    def counts(self):
        db = self.Session()
        try:
            return {
                "departments": db.query(models.Department).count(),
                "issues": db.query(models.Issue).count(),
                "users": db.query(models.User).count(),
                "status_updates": db.query(models.IssueStatusUpdate).count(),
                "images": db.query(models.IssueImage).count(),
            }
        finally:
            db.close()

    def test_bootstrap_targets_the_isolated_database(self):
        # Guards against silently patching the wrong module attribute.
        import app.bootstrap as bootstrap_module

        self.assertIs(bootstrap_module.engine, self.engine)
        self.assertIs(bootstrap_module.SessionLocal, self.Session)

    def test_no_departments_are_seeded_on_an_empty_database(self):
        ensure_bootstrap()
        self.assertEqual(self.counts()["departments"], 0)

    def test_no_placeholder_departments_or_contacts_are_created(self):
        ensure_bootstrap()
        db = self.Session()
        try:
            self.assertEqual(db.query(models.Department).count(), 0)
        finally:
            db.close()

    def test_no_complaints_or_demo_records_are_created(self):
        ensure_bootstrap()
        counts = self.counts()
        self.assertEqual(counts["issues"], 0)
        self.assertEqual(counts["status_updates"], 0)
        self.assertEqual(counts["images"], 0)

    def test_existing_verified_department_is_preserved(self):
        db = self.Session()
        try:
            db.add(models.Department(
                name="BBMP - Roads and Footpaths",
                handles_category=models.IssueCategory.ROAD_DAMAGE,
                city="Bengaluru, Karnataka, India",
                source_agency="BBMP",
                contact_email=None,
                contact_phone=None,
            ))
            db.commit()
        finally:
            db.close()

        ensure_bootstrap()
        ensure_bootstrap()

        db = self.Session()
        try:
            rows = db.query(models.Department).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].name, "BBMP - Roads and Footpaths")
            self.assertEqual(rows[0].city, "Bengaluru, Karnataka, India")
            self.assertEqual(rows[0].source_agency, "BBMP")
            self.assertIsNone(rows[0].contact_email)
            self.assertIsNone(rows[0].contact_phone)
        finally:
            db.close()

    def test_system_user_bootstrap_is_unchanged_and_idempotent(self):
        ensure_bootstrap()
        ensure_bootstrap()
        db = self.Session()
        try:
            users = db.query(models.User).all()
            self.assertEqual(len(users), 1)
            self.assertEqual(users[0].email, SYSTEM_USER_EMAIL)
            self.assertEqual(users[0].role, models.UserRole.ADMIN)
        finally:
            db.close()

    def test_frontend_and_candidate_environment_do_not_affect_departments(self):
        # Under the old code, UIMRS_CANDIDATE_MODE != "1" seeded nine
        # placeholder departments. It must now have no effect at all.
        environments = [
            {},
            {"UIMRS_CANDIDATE_MODE": "0"},
            {"UIMRS_CANDIDATE_MODE": "1"},
            {"UIMRS_FRONTEND_MODE": "legacy"},
            {"UIMRS_FRONTEND_MODE": "india"},
        ]
        for env in environments:
            with self.subTest(env=env):
                with patch.dict(os.environ, env, clear=True):
                    ensure_bootstrap()
                self.assertEqual(self.counts()["departments"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
