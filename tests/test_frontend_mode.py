"""
UIMRS Task 2 — Frontend mode selection and route-level write access.

Verifies that:
1. ``UIMRS_FRONTEND_MODE`` selects the India or legacy page, with the old
   ``UIMRS_CANDIDATE_MODE`` kept only as a backward-compatible alias.
2. Frontend selection no longer disables API writes: registration, login,
   chatbot and authorized issue operations work while candidate mode is set.
3. Route-level authentication/RBAC and historical-record protections are intact.

Runs against an ISOLATED in-memory SQLite database. No real project database
(``uimrs.db``, ``uimrs_india_candidate.db``, ``uimrs_india_dev.db``) is opened.
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

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.core.config import (
    DEFAULT_FRONTEND_MODE,
    FRONTEND_DIR,
    FRONTEND_PAGES,
    FrontendModeError,
    frontend_dashboard_path,
    resolve_frontend_mode,
)
from app.database import Base, get_db
from app.main import app
from app.routers.auth import create_access_token, hash_password
from app.services.classification_service import ClassificationResult, PriorityResult

INDIA_PAGE = FRONTEND_DIR / FRONTEND_PAGES["india"]
LEGACY_PAGE = FRONTEND_DIR / FRONTEND_PAGES["legacy"]

TEST_PASSWORD = "FrontendMode#2026"


class TestFrontendModeSelection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_default_mode_is_india(self):
        self.assertEqual(DEFAULT_FRONTEND_MODE, "india")
        self.assertEqual(resolve_frontend_mode({}), "india")
        self.assertEqual(frontend_dashboard_path(environ={}), INDIA_PAGE)

    def test_default_frontend_served_by_root_endpoint(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, INDIA_PAGE.read_bytes())

    def test_explicit_india_mode(self):
        env = {"UIMRS_FRONTEND_MODE": "india"}
        self.assertEqual(resolve_frontend_mode(env), "india")
        with patch.dict(os.environ, env, clear=True):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, INDIA_PAGE.read_bytes())

    def test_explicit_legacy_mode(self):
        env = {"UIMRS_FRONTEND_MODE": "legacy"}
        self.assertEqual(resolve_frontend_mode(env), "legacy")
        self.assertEqual(frontend_dashboard_path(environ=env), LEGACY_PAGE)
        with patch.dict(os.environ, env, clear=True):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, LEGACY_PAGE.read_bytes())

    def test_mode_is_case_insensitive(self):
        self.assertEqual(resolve_frontend_mode({"UIMRS_FRONTEND_MODE": "INDIA"}), "india")
        self.assertEqual(resolve_frontend_mode({"UIMRS_FRONTEND_MODE": " Legacy "}), "legacy")

    def test_new_setting_takes_precedence_over_candidate_mode(self):
        env = {"UIMRS_FRONTEND_MODE": "legacy", "UIMRS_CANDIDATE_MODE": "1"}
        self.assertEqual(resolve_frontend_mode(env), "legacy")
        env = {"UIMRS_FRONTEND_MODE": "india", "UIMRS_CANDIDATE_MODE": "0"}
        self.assertEqual(resolve_frontend_mode(env), "india")

    def test_candidate_mode_alias_still_selects_frontend(self):
        self.assertEqual(resolve_frontend_mode({"UIMRS_CANDIDATE_MODE": "1"}), "india")
        self.assertEqual(resolve_frontend_mode({"UIMRS_CANDIDATE_MODE": "0"}), "legacy")
        self.assertEqual(resolve_frontend_mode({"UIMRS_CANDIDATE_MODE": "true"}), "india")

    def test_candidate_mode_alias_serves_india_page(self):
        with patch.dict(os.environ, {"UIMRS_CANDIDATE_MODE": "1"}, clear=True):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, INDIA_PAGE.read_bytes())

    def test_invalid_frontend_mode_raises_clearly(self):
        with self.assertRaises(FrontendModeError) as ctx:
            resolve_frontend_mode({"UIMRS_FRONTEND_MODE": "nyc"})
        self.assertIn("UIMRS_FRONTEND_MODE", str(ctx.exception))
        self.assertIn("india", str(ctx.exception))
        self.assertIn("legacy", str(ctx.exception))

    def test_invalid_candidate_alias_raises_clearly(self):
        with self.assertRaises(FrontendModeError) as ctx:
            resolve_frontend_mode({"UIMRS_CANDIDATE_MODE": "staging"})
        self.assertIn("UIMRS_CANDIDATE_MODE", str(ctx.exception))

    def test_invalid_mode_returns_configuration_error_not_a_page(self):
        with patch.dict(os.environ, {"UIMRS_FRONTEND_MODE": "nyc"}, clear=True):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 500)
        self.assertIn("Frontend configuration error", response.json()["detail"])

    def test_startup_rejects_invalid_mode(self):
        from app.main import validate_frontend_configuration

        with patch.dict(os.environ, {"UIMRS_FRONTEND_MODE": "nyc"}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                validate_frontend_configuration()
        self.assertIn("Frontend configuration error", str(ctx.exception))

    def test_startup_accepts_valid_mode(self):
        from app.main import validate_frontend_configuration

        with patch.dict(os.environ, {"UIMRS_FRONTEND_MODE": "legacy"}, clear=True):
            validate_frontend_configuration()  # must not raise

    def test_frontend_selection_does_not_touch_database_settings(self):
        from app.core.config import default_database_url

        before = default_database_url()
        resolve_frontend_mode({"UIMRS_FRONTEND_MODE": "legacy"})
        self.assertEqual(default_database_url(), before)


# ---------------------------------------------------------------------------
# API write access with candidate mode enabled (previously blocked globally)
# ---------------------------------------------------------------------------
TEST_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


class TestWritesNotBlockedByFrontendMode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)

    def setUp(self):
        self.db = TestingSessionLocal()
        self.old_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_db] = override_get_db

        self.db.query(models.IssueStatusUpdate).delete()
        self.db.query(models.IssueImage).delete()
        self.db.query(models.Issue).delete()
        self.db.query(models.User).delete()
        self.db.query(models.Department).delete()
        self.db.commit()

        self.dept_roads = models.Department(
            name="TEST_FM_ROADS",
            handles_category=models.IssueCategory.ROAD_DAMAGE,
        )
        self.dept_garbage = models.Department(
            name="TEST_FM_GARBAGE",
            handles_category=models.IssueCategory.GARBAGE_WASTE,
        )
        self.db.add_all([self.dept_roads, self.dept_garbage])
        self.db.commit()

        self.citizen = models.User(
            name="TEST_FM_CITIZEN",
            email="fm.citizen@uimrs.org",
            password_hash=hash_password(TEST_PASSWORD),
            role=models.UserRole.CITIZEN,
        )
        self.officer_roads = models.User(
            name="TEST_FM_OFFICER",
            email="fm.officer.roads@uimrs.org",
            password_hash=hash_password(TEST_PASSWORD),
            role=models.UserRole.OFFICER,
            department_id=self.dept_roads.id,
        )
        self.admin = models.User(
            name="TEST_FM_ADMIN",
            email="fm.admin@uimrs.org",
            password_hash=hash_password(TEST_PASSWORD),
            role=models.UserRole.ADMIN,
        )
        self.db.add_all([self.citizen, self.officer_roads, self.admin])
        self.db.commit()

        self.token_citizen = create_access_token(self.citizen)
        self.token_officer = create_access_token(self.officer_roads)
        self.token_admin = create_access_token(self.admin)

    def tearDown(self):
        self.db.close()
        app.dependency_overrides = self.old_overrides

    @staticmethod
    def candidate_mode():
        """Candidate mode plus India frontend, as used for the staging demo."""
        return patch.dict(
            os.environ,
            {"UIMRS_CANDIDATE_MODE": "1", "UIMRS_FRONTEND_MODE": "india"},
            clear=True,
        )

    def _live_issue(self, department, category=models.IssueCategory.ROAD_DAMAGE):
        issue = models.Issue(
            ticket_number="TEST-FM-001",
            title="Live test issue",
            description="Live issue used for route-level authorization checks.",
            category=category,
            status=models.IssueStatus.REPORTED,
            latitude=28.4601,
            longitude=77.0648,
            reporter_id=self.citizen.id,
            department_id=department.id,
            assigned_officer_id=self.officer_roads.id,
            routing_status="assigned",
        )
        self.db.add(issue)
        self.db.commit()
        return issue

    def _historical_issue(self):
        issue = models.Issue(
            ticket_number="HIST-FM-001",
            title="Historical test fixture",
            description="Historical fixture used for immutability checks.",
            category=models.IssueCategory.ROAD_DAMAGE,
            status=models.IssueStatus.RESOLVED,
            latitude=12.9716,
            longitude=77.5946,
            reporter_id=self.citizen.id,
            is_historical=True,
            source_record_id="FIXTURE-ONLY",
        )
        self.db.add(issue)
        self.db.commit()
        return issue

    # --- public endpoints -------------------------------------------------

    def test_registration_not_blocked(self):
        with self.candidate_mode():
            response = self.client.post(
                "/api/v1/auth/register",
                json={
                    "name": "New Citizen",
                    "email": "fm.new.citizen@uimrs.org",
                    "password": TEST_PASSWORD,
                },
            )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["role"], "citizen")
        self.assertNotIn("password", response.json())

    def test_login_not_blocked(self):
        with self.candidate_mode():
            response = self.client.post(
                "/api/v1/auth/login",
                json={"email": self.citizen.email, "password": TEST_PASSWORD},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("access_token", response.json())
        self.assertEqual(response.json()["token_type"], "bearer")

    def test_chatbot_not_blocked(self):
        with self.candidate_mode():
            response = self.client.post(
                "/api/v1/chatbot/message",
                json={"message": "How do I track my complaint status?"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("intent", body)
        self.assertIn("response", body)

    # --- issue creation ---------------------------------------------------

    @patch("app.routers.issues.priority_engine.score")
    @patch("app.routers.issues.classifier.classify")
    def test_authenticated_citizen_submission_reaches_route(self, mock_classify, mock_priority):
        mock_classify.return_value = ClassificationResult(
            category=models.IssueCategory.ROAD_DAMAGE, confidence=0.9, source="test"
        )
        mock_priority.return_value = PriorityResult(score=5.0, level="Medium", reasons=[])

        payload = {
            "title": "Candidate-mode pothole report",
            "description": "Deep pothole on the main road, dangerous for two-wheelers.",
            "latitude": 28.4601,
            "longitude": 77.0648,
            "city": "Gurugram, Haryana, India",
        }
        with self.candidate_mode():
            response = self.client.post(
                "/api/v1/issues",
                json=payload,
                headers={"Authorization": f"Bearer {self.token_citizen}"},
            )
        self.assertEqual(response.status_code, 201, response.text)
        issue = self.db.query(models.Issue).filter(
            models.Issue.id == response.json()["id"]
        ).first()
        self.assertIsNotNone(issue)
        self.assertEqual(issue.reporter_id, self.citizen.id)

    def test_anonymous_submission_still_unauthorized(self):
        payload = {
            "title": "Anonymous candidate-mode report",
            "description": "This must stay unauthorized without a token.",
            "latitude": 28.4601,
            "longitude": 77.0648,
            "city": "Gurugram, Haryana, India",
        }
        with self.candidate_mode():
            response = self.client.post("/api/v1/issues", json=payload)
        self.assertEqual(response.status_code, 401)

    def test_no_global_method_block_remains(self):
        """An unrouted POST must 404, not the old blanket 405."""
        with self.candidate_mode():
            response = self.client.post("/api/v1/definitely-not-a-route", json={})
        self.assertEqual(response.status_code, 404)

    # --- status updates ---------------------------------------------------

    def test_unauthorized_officer_status_update_still_blocked(self):
        issue = self._live_issue(self.dept_garbage, models.IssueCategory.GARBAGE_WASTE)
        with self.candidate_mode():
            response = self.client.patch(
                f"/api/v1/issues/{issue.id}/status",
                json={"new_status": "acknowledged"},
                headers={"Authorization": f"Bearer {self.token_officer}"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertIn("not authorized", response.json()["detail"])

    def test_authorized_same_department_officer_update_permitted(self):
        issue = self._live_issue(self.dept_roads)
        with self.candidate_mode():
            response = self.client.patch(
                f"/api/v1/issues/{issue.id}/status",
                json={"new_status": "in_progress", "comment": "Crew dispatched"},
                headers={"Authorization": f"Bearer {self.token_officer}"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        audit = self.db.query(models.IssueStatusUpdate).filter(
            models.IssueStatusUpdate.issue_id == issue.id
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.updated_by_id, self.officer_roads.id)

    def test_historical_status_update_still_blocked(self):
        issue = self._historical_issue()
        with self.candidate_mode():
            response = self.client.patch(
                f"/api/v1/issues/{issue.id}/status",
                json={"new_status": "reported"},
                headers={"Authorization": f"Bearer {self.token_admin}"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertIn("immutable", response.json()["detail"])
        self.db.refresh(issue)
        self.assertEqual(issue.status, models.IssueStatus.RESOLVED)

    def test_historical_image_upload_still_blocked(self):
        issue = self._historical_issue()
        with self.candidate_mode():
            response = self.client.post(
                f"/api/v1/issues/{issue.id}/images",
                files={"file": ("evidence.jpg", b"not-a-real-image", "image/jpeg")},
                headers={"Authorization": f"Bearer {self.token_citizen}"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertIn("historical", response.json()["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
