"""UIMRS Task 7 — Citizen portal API, tracking and frontend contract tests.

Runs against an isolated in-memory SQLite database and a temporary upload
directory. The real project databases are never opened.
"""
import re
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import cv2
import numpy as np
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.core.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.routers.auth import create_access_token, hash_password
from app.services.classification_service import ClassificationResult, PriorityResult

PORTAL_PAGE = PROJECT_ROOT / "frontend" / "citizen_portal.html"
HISTORICAL_PAGE = PROJECT_ROOT / "frontend" / "index_india_staging.html"

TEST_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

TEST_PASSWORD = "CitizenPortal#2026"


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def make_image_bytes(extension=".jpg"):
    image = np.full((400, 400, 3), 130, dtype=np.uint8)
    image[::20, :] = 255
    image[:, ::20] = 255
    image[10::20, :] = 0
    image[:, 10::20] = 0
    ok, buffer = cv2.imencode(extension, image)
    if not ok:
        raise RuntimeError(f"Could not encode test image as {extension}")
    return buffer.tobytes()


class CitizenPortalTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.password_hash = hash_password(TEST_PASSWORD)
        cls.settings = get_settings()

    def setUp(self):
        self.db = TestingSessionLocal()
        self.old_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_db] = override_get_db

        self.original_upload_dir = self.settings.UPLOAD_DIR
        self.upload_dir = tempfile.mkdtemp(prefix="uimrs_citizen_evidence_")
        self.settings.UPLOAD_DIR = self.upload_dir

        self.db.query(models.IssueStatusUpdate).delete()
        self.db.query(models.IssueImage).delete()
        self.db.query(models.Issue).delete()
        self.db.query(models.User).delete()
        self.db.query(models.Department).delete()
        self.db.commit()

        self._setup_fixtures()

    def tearDown(self):
        self.db.close()
        app.dependency_overrides = self.old_overrides
        self.settings.UPLOAD_DIR = self.original_upload_dir
        shutil.rmtree(self.upload_dir, ignore_errors=True)

    def _user(self, name, email, role, department_id=None):
        return models.User(
            name=name,
            email=email,
            password_hash=self.password_hash,
            role=role,
            department_id=department_id,
        )

    def _issue(self, ticket, reporter, department, **overrides):
        values = {
            "title": "Fixture complaint",
            "description": "Fixture description used only in isolated tests.",
            "category": models.IssueCategory.ROAD_DAMAGE,
            "status": models.IssueStatus.REPORTED,
            "latitude": 28.4595,
            "longitude": 77.0266,
            "city": "Gurugram, Haryana, India",
            "reporter_id": reporter.id,
            "department_id": department.id if department else None,
            "is_historical": False,
        }
        values.update(overrides)
        issue = models.Issue(ticket_number=ticket, **values)
        self.db.add(issue)
        self.db.commit()
        self.db.refresh(issue)
        return issue

    def _setup_fixtures(self):
        self.dept_roads = models.Department(
            name="TEST_PORTAL_ROADS",
            handles_category=models.IssueCategory.ROAD_DAMAGE,
            city="Gurugram, Haryana, India",
        )
        self.dept_garbage = models.Department(
            name="TEST_PORTAL_GARBAGE",
            handles_category=models.IssueCategory.GARBAGE_WASTE,
            city="Gurugram, Haryana, India",
        )
        self.db.add_all([self.dept_roads, self.dept_garbage])
        self.db.commit()

        self.citizen_a = self._user("Citizen Alpha", "alpha@example.com", models.UserRole.CITIZEN)
        self.citizen_b = self._user("Citizen Beta", "beta@example.com", models.UserRole.CITIZEN)
        self.officer_roads = self._user(
            "Road Officer", "roads.officer@example.com", models.UserRole.OFFICER, self.dept_roads.id
        )
        self.officer_garbage = self._user(
            "Garbage Officer", "garbage.officer@example.com", models.UserRole.OFFICER, self.dept_garbage.id
        )
        self.admin = self._user("Portal Admin", "admin@example.com", models.UserRole.ADMIN)
        self.db.add_all([self.citizen_a, self.citizen_b, self.officer_roads, self.officer_garbage, self.admin])
        self.db.commit()

        self.token_a = create_access_token(self.citizen_a)
        self.token_b = create_access_token(self.citizen_b)
        self.token_roads = create_access_token(self.officer_roads)
        self.token_garbage = create_access_token(self.officer_garbage)
        self.token_admin = create_access_token(self.admin)

        self.issue_a = self._issue("PORTAL-TEST-001", self.citizen_a, self.dept_roads)
        self.issue_b = self._issue(
            "PORTAL-TEST-002",
            self.citizen_b,
            self.dept_garbage,
            category=models.IssueCategory.GARBAGE_WASTE,
            title="Fixture garbage complaint",
        )
        self.historical = self._issue(
            "PORTAL-HIST-001",
            self.citizen_a,
            self.dept_roads,
            status=models.IssueStatus.RESOLVED,
            is_historical=True,
            source_record_id="HIST-001",
            title="Historical fixture record",
        )


class TestCitizenPortalRoute(CitizenPortalTestBase):
    def test_citizen_route_serves_portal(self):
        response = self.client.get("/citizen")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PORTAL_PAGE.read_bytes())
        self.assertIn("text/html", response.headers.get("content-type", ""))

    def test_historical_dashboard_route_is_unchanged(self):
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, HISTORICAL_PAGE.read_bytes())

    def test_frontend_contracts_match_backend(self):
        page = PORTAL_PAGE.read_text(encoding="utf-8")
        for endpoint in (
            "/auth/register",
            "/auth/login",
            "/auth/me",
            "/issues",
            "/history",
            "/images",
            "/chatbot/message",
        ):
            self.assertIn(endpoint, page, f"missing endpoint contract: {endpoint}")

        self.assertIn("Gurugram, Haryana, India", page)
        self.assertIn("Bengaluru, Karnataka, India", page)
        self.assertIn("sessionStorage", page)
        self.assertIn("textContent", page)
        self.assertIn("Use my current location", page)
        self.assertIn("uncalibrated", page)
        self.assertIn("cannot look up, create or update", page)

    def test_frontend_uses_safe_dom_apis_only(self):
        page = PORTAL_PAGE.read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", page)
        self.assertNotIn("outerHTML", page)
        self.assertNotIn("insertAdjacentHTML", page)
        self.assertNotIn("document.write", page)
        self.assertNotIn("Math.random", page)
        self.assertNotIn("localStorage", page)

    def test_frontend_does_not_embed_fabricated_tickets(self):
        page = PORTAL_PAGE.read_text(encoding="utf-8")
        # No hard-coded ticket values or sample complaint rows.
        self.assertIsNone(re.search(r"UIMRS-\d{4}-\d{3,}", page))
        self.assertNotIn("sample complaint", page.lower())
        self.assertNotIn("demo complaint", page.lower())


class TestCitizenAuthentication(CitizenPortalTestBase):
    def test_public_registration_creates_citizen_only(self):
        response = self.client.post(
            "/api/v1/auth/register",
            json={
                "name": "New Citizen",
                "email": "new.citizen@example.com",
                "phone": "+91 90000 00001",
                "password": "NewCitizen#2026",
                "preferred_language": "en",
                # Unsupported privilege fields must be ignored, never honoured.
                "role": "admin",
                "department_id": self.dept_roads.id,
            },
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["role"], "citizen")
        self.assertIsNone(data["department_id"])

        user = self.db.query(models.User).filter(models.User.email == "new.citizen@example.com").first()
        self.assertIsNotNone(user)
        self.assertEqual(user.role, models.UserRole.CITIZEN)
        self.assertNotIn("NewCitizen#2026", user.password_hash)

    def test_duplicate_email_rejected(self):
        response = self.client.post(
            "/api/v1/auth/register",
            json={
                "name": "Duplicate",
                "email": "alpha@example.com",
                "password": "Duplicate#2026",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("already registered", response.json()["detail"])

    def test_short_password_rejected_by_schema(self):
        response = self.client.post(
            "/api/v1/auth/register",
            json={"name": "Short", "email": "short@example.com", "password": "short"},
        )
        self.assertEqual(response.status_code, 422)

    def test_login_success_and_me(self):
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": "alpha@example.com", "password": TEST_PASSWORD},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["token_type"], "bearer")
        self.assertTrue(data["access_token"])
        self.assertEqual(data["user"]["email"], "alpha@example.com")

        me = self.client.get("/api/v1/auth/me", headers=bearer(data["access_token"]))
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["id"], self.citizen_a.id)

    def test_login_failure(self):
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": "alpha@example.com", "password": "WrongPassword#2026"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Invalid credentials")

    def test_invalid_token_returns_401(self):
        response = self.client.get("/api/v1/auth/me", headers=bearer("not-a-valid-jwt"))
        self.assertEqual(response.status_code, 401)

    def test_expired_token_returns_401(self):
        expired = create_access_token(self.citizen_a, expires_delta=timedelta(seconds=-5))
        response = self.client.get("/api/v1/auth/me", headers=bearer(expired))
        self.assertEqual(response.status_code, 401)
        self.assertIn("expired", response.json()["detail"].lower())


class TestCitizenReporting(CitizenPortalTestBase):
    def test_anonymous_submission_rejected(self):
        response = self.client.post(
            "/api/v1/issues",
            json={
                "title": "Anonymous complaint",
                "description": "This request has no bearer token.",
                "city": "Gurugram, Haryana, India",
                "latitude": 28.4595,
                "longitude": 77.0266,
            },
        )
        self.assertEqual(response.status_code, 401)

    @patch("app.routers.issues.classifier.classify")
    @patch("app.routers.issues.priority_engine.score")
    def test_authenticated_submission_persists_explainability(self, mock_priority, mock_classify):
        mock_classify.return_value = ClassificationResult(
            category=models.IssueCategory.ROAD_DAMAGE,
            confidence=0.64,
            source="india_keyword_rule",
            needs_review=False,
        )
        mock_priority.return_value = PriorityResult(
            score=6.5, level="High", reasons=["Main-road location", "Safety risk"]
        )

        response = self.client.post(
            "/api/v1/issues",
            headers=bearer(self.token_a),
            json={
                "title": "Deep pothole near Sector 29",
                "description": "A deep pothole is blocking the left lane near the market entrance.",
                "city": "Gurugram, Haryana, India",
                "latitude": 28.4601,
                "longitude": 77.0648,
                "address_text": "Near Sector 29 market",
            },
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertTrue(data["ticket_number"].startswith("UIMRS-"))
        self.assertEqual(data["reporter_id"], self.citizen_a.id)
        self.assertEqual(data["category_source"], "india_keyword_rule")
        self.assertEqual(data["category_score_kind"], "rule_heuristic")
        self.assertEqual(data["category_confidence"], 0.64)
        self.assertFalse(data["category_needs_review"])
        self.assertEqual(data["priority_level"], "High")
        self.assertEqual(data["priority_score"], 6.5)
        self.assertEqual(data["priority_reasons"], ["Main-road location", "Safety risk"])

        stored = self.db.query(models.Issue).filter(models.Issue.id == data["id"]).first()
        self.assertEqual(stored.reporter_id, self.citizen_a.id)
        self.assertEqual(stored.category_source, "india_keyword_rule")

    @patch("app.routers.issues.classifier.classify")
    @patch("app.routers.issues.priority_engine.score")
    def test_bengaluru_ml_classification_is_recorded_honestly(self, mock_priority, mock_classify):
        mock_classify.return_value = ClassificationResult(
            category=models.IssueCategory.ROAD_DAMAGE,
            confidence=0.72,
            source="bengaluru_v2_2_ml",
            needs_review=True,
            review_reason="Top alternatives are close.",
            model_version="test_bengaluru_v2_2",
            top_alternatives=[
                {"category": "Road_Damage", "model_score": 0.72},
                {"category": "Public_Property_Damage", "model_score": 0.61},
            ],
        )
        mock_priority.return_value = PriorityResult(score=4.0, level="Medium", reasons=["Reported hazard"])

        response = self.client.post(
            "/api/v1/issues",
            headers=bearer(self.token_a),
            json={
                "title": "Damaged road surface",
                "description": "The road surface is broken and dangerous for two-wheelers.",
                "city": "Bengaluru, Karnataka, India",
                "latitude": 12.9716,
                "longitude": 77.5946,
            },
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["city"], "Bengaluru, Karnataka, India")
        self.assertEqual(data["category_source"], "bengaluru_v2_2_ml")
        self.assertEqual(data["category_score_kind"], "uncalibrated_model_score")
        self.assertTrue(data["category_needs_review"])
        self.assertEqual(data["category_review_reason"], "Top alternatives are close.")
        self.assertEqual(data["category_model_version"], "test_bengaluru_v2_2")
        self.assertEqual(len(data["category_top_alternatives"]), 2)

    @patch("app.routers.issues.classifier.classify")
    @patch("app.routers.issues.priority_engine.score")
    def test_forged_reporter_id_is_ignored(self, mock_priority, mock_classify):
        mock_classify.return_value = ClassificationResult(
            category=models.IssueCategory.GARBAGE_WASTE,
            confidence=0.5,
            source="india_keyword_rule",
        )
        mock_priority.return_value = PriorityResult(score=1.0, level="Low", reasons=[])

        response = self.client.post(
            "/api/v1/issues",
            headers=bearer(self.token_a),
            json={
                "title": "Garbage pile",
                "description": "A garbage pile has been left beside the road.",
                "city": "Gurugram, Haryana, India",
                "latitude": 28.4595,
                "longitude": 77.0266,
                "reporter_id": self.citizen_b.id,
            },
        )
        self.assertEqual(response.status_code, 201)
        stored = self.db.query(models.Issue).filter(models.Issue.id == response.json()["id"]).first()
        self.assertEqual(stored.reporter_id, self.citizen_a.id)

    def test_unsupported_city_rejected(self):
        response = self.client.post(
            "/api/v1/issues",
            headers=bearer(self.token_a),
            json={
                "title": "Foreign city complaint",
                "description": "This city is not an operating city for UIMRS.",
                "city": "New York, USA",
                "latitude": 40.7128,
                "longitude": -74.0060,
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("Unsupported city", response.text)

    def test_coordinates_outside_city_rejected(self):
        response = self.client.post(
            "/api/v1/issues",
            headers=bearer(self.token_a),
            json={
                "title": "Out of bounds complaint",
                "description": "These coordinates are outside the Gurugram boundary.",
                "city": "Gurugram, Haryana, India",
                "latitude": 12.9716,
                "longitude": 77.5946,
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("outside the municipal boundary", response.text)

    def test_short_title_and_description_rejected(self):
        response = self.client.post(
            "/api/v1/issues",
            headers=bearer(self.token_a),
            json={
                "title": "Bad",
                "description": "short",
                "city": "Gurugram, Haryana, India",
                "latitude": 28.4595,
                "longitude": 77.0266,
            },
        )
        self.assertEqual(response.status_code, 422)


class TestCitizenTracking(CitizenPortalTestBase):
    def test_citizen_sees_only_own_live_complaints(self):
        response = self.client.get("/api/v1/issues?limit=50", headers=bearer(self.token_a))
        self.assertEqual(response.status_code, 200)
        tickets = {row["ticket_number"] for row in response.json()}
        self.assertIn(self.issue_a.ticket_number, tickets)
        self.assertNotIn(self.issue_b.ticket_number, tickets)
        self.assertNotIn(self.historical.ticket_number, tickets)
        self.assertTrue(all(row["is_historical"] is False for row in response.json()))

    def test_citizen_cannot_read_another_citizens_issue(self):
        response = self.client.get(f"/api/v1/issues/{self.issue_b.id}", headers=bearer(self.token_a))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Issue not found")

    def test_citizen_cannot_read_historical_record(self):
        response = self.client.get(f"/api/v1/issues/{self.historical.id}", headers=bearer(self.token_a))
        self.assertEqual(response.status_code, 404)

    def test_own_issue_detail_returns_explainability_fields(self):
        response = self.client.get(f"/api/v1/issues/{self.issue_a.id}", headers=bearer(self.token_a))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["ticket_number"], self.issue_a.ticket_number)
        for key in (
            "category_source",
            "category_score_kind",
            "category_needs_review",
            "category_review_reason",
            "category_top_alternatives",
            "priority_reasons",
        ):
            self.assertIn(key, data)

    def test_status_history_is_scoped(self):
        update = models.IssueStatusUpdate(
            issue_id=self.issue_a.id,
            updated_by_id=self.officer_roads.id,
            old_status=models.IssueStatus.REPORTED,
            new_status=models.IssueStatus.ACKNOWLEDGED,
            comment="Inspection scheduled.",
        )
        self.db.add(update)
        self.db.commit()

        response = self.client.get(
            f"/api/v1/issues/{self.issue_a.id}/history", headers=bearer(self.token_a)
        )
        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["new_status"], "acknowledged")
        self.assertEqual(rows[0]["comment"], "Inspection scheduled.")

        denied = self.client.get(
            f"/api/v1/issues/{self.issue_a.id}/history", headers=bearer(self.token_b)
        )
        self.assertEqual(denied.status_code, 404)

    def test_empty_state_has_no_fabricated_rows(self):
        empty_citizen = self._user("Empty Citizen", "empty@example.com", models.UserRole.CITIZEN)
        self.db.add(empty_citizen)
        self.db.commit()
        token = create_access_token(empty_citizen)

        response = self.client.get("/api/v1/issues", headers=bearer(token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_status_and_category_filters(self):
        self._issue(
            "PORTAL-TEST-003",
            self.citizen_a,
            self.dept_roads,
            status=models.IssueStatus.RESOLVED,
            category=models.IssueCategory.ROAD_DAMAGE,
            title="Resolved road complaint",
        )
        self._issue(
            "PORTAL-TEST-004",
            self.citizen_a,
            self.dept_garbage,
            status=models.IssueStatus.REJECTED,
            category=models.IssueCategory.GARBAGE_WASTE,
            title="Rejected garbage complaint",
        )

        resolved = self.client.get(
            "/api/v1/issues?status=resolved", headers=bearer(self.token_a)
        )
        self.assertEqual(resolved.status_code, 200)
        self.assertTrue(resolved.json())
        self.assertTrue(all(row["status"] == "resolved" for row in resolved.json()))

        garbage = self.client.get(
            "/api/v1/issues?category=Garbage_Waste", headers=bearer(self.token_a)
        )
        self.assertEqual(garbage.status_code, 200)
        self.assertTrue(garbage.json())
        self.assertTrue(all(row["category"] == "Garbage_Waste" for row in garbage.json()))

    def test_bounded_pagination(self):
        pager = self._user("Pager Citizen", "pager@example.com", models.UserRole.CITIZEN)
        self.db.add(pager)
        self.db.commit()
        token = create_access_token(pager)
        for index in range(3):
            self._issue(f"PORTAL-PAGE-{index}", pager, self.dept_roads)

        first = self.client.get("/api/v1/issues?skip=0&limit=2", headers=bearer(token))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(first.json()), 2)

        second = self.client.get("/api/v1/issues?skip=2&limit=2", headers=bearer(token))
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(second.json()), 1)

    def test_anonymous_list_rejected(self):
        response = self.client.get("/api/v1/issues")
        self.assertEqual(response.status_code, 401)


class TestCitizenEvidence(CitizenPortalTestBase):
    def _upload(self, issue_id, token, data=None, filename="evidence.jpg", content_type="image/jpeg"):
        return self.client.post(
            f"/api/v1/issues/{issue_id}/images",
            files={"file": (filename, data if data is not None else make_image_bytes(), content_type)},
            headers=bearer(token),
        )

    def test_valid_upload_and_authorised_download(self):
        image_bytes = make_image_bytes()
        response = self._upload(self.issue_a.id, self.token_a, image_bytes)
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertTrue(data["image_url"].startswith("evidence/"))
        self.assertTrue(data["image_url"].endswith(".jpg"))
        self.assertNotIn("evidence.jpg", data["image_url"])
        self.assertTrue(data["passed_verification"])

        stored_path = Path(self.upload_dir) / data["image_url"]
        self.assertTrue(stored_path.is_file())

        listing = self.client.get(
            f"/api/v1/issues/{self.issue_a.id}/images", headers=bearer(self.token_a)
        )
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.json()), 1)
        image_id = listing.json()[0]["id"]

        content = self.client.get(
            f"/api/v1/issues/{self.issue_a.id}/images/{image_id}/content",
            headers=bearer(self.token_a),
        )
        self.assertEqual(content.status_code, 200)
        self.assertEqual(content.content, image_bytes)
        self.assertIn("image/jpeg", content.headers.get("content-type", ""))
        self.assertEqual(content.headers.get("cache-control"), "private, no-store")

    def test_other_citizen_cannot_read_evidence(self):
        upload = self._upload(self.issue_a.id, self.token_a)
        self.assertEqual(upload.status_code, 201)
        image_id = upload.json()["id"]

        content = self.client.get(
            f"/api/v1/issues/{self.issue_a.id}/images/{image_id}/content",
            headers=bearer(self.token_b),
        )
        self.assertEqual(content.status_code, 404)

        listing = self.client.get(
            f"/api/v1/issues/{self.issue_a.id}/images", headers=bearer(self.token_b)
        )
        self.assertEqual(listing.status_code, 404)

    def test_officer_department_scoping(self):
        upload = self._upload(self.issue_a.id, self.token_a)
        self.assertEqual(upload.status_code, 201)
        image_id = upload.json()["id"]

        same_department = self.client.get(
            f"/api/v1/issues/{self.issue_a.id}/images/{image_id}/content",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(same_department.status_code, 200)

        other_department = self.client.get(
            f"/api/v1/issues/{self.issue_a.id}/images/{image_id}/content",
            headers=bearer(self.token_garbage),
        )
        self.assertEqual(other_department.status_code, 404)

    def test_admin_can_read_evidence(self):
        upload = self._upload(self.issue_a.id, self.token_a)
        self.assertEqual(upload.status_code, 201)
        image_id = upload.json()["id"]
        response = self.client.get(
            f"/api/v1/issues/{self.issue_a.id}/images/{image_id}/content",
            headers=bearer(self.token_admin),
        )
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_evidence_access_rejected(self):
        upload = self._upload(self.issue_a.id, self.token_a)
        self.assertEqual(upload.status_code, 201)
        image_id = upload.json()["id"]
        response = self.client.get(f"/api/v1/issues/{self.issue_a.id}/images/{image_id}/content")
        self.assertEqual(response.status_code, 401)

    def test_citizen_cannot_upload_to_another_issue(self):
        response = self._upload(self.issue_a.id, self.token_b)
        self.assertEqual(response.status_code, 403)

    def test_historical_upload_rejected(self):
        response = self._upload(self.historical.id, self.token_a)
        self.assertEqual(response.status_code, 403)
        self.assertIn("historical", response.json()["detail"].lower())
        evidence_dir = Path(self.upload_dir) / "evidence"
        self.assertFalse(evidence_dir.exists() and any(evidence_dir.iterdir()))

    def test_legacy_absolute_path_is_not_served(self):
        legacy_file = Path(self.upload_dir) / "legacy.jpg"
        legacy_file.write_bytes(make_image_bytes())
        image = models.IssueImage(
            issue_id=self.issue_a.id,
            image_url=str(legacy_file.resolve()),
            passed_verification=True,
            blur_score=1.0,
            brightness=120.0,
        )
        self.db.add(image)
        self.db.commit()

        response = self.client.get(
            f"/api/v1/issues/{self.issue_a.id}/images/{image.id}/content",
            headers=bearer(self.token_a),
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
