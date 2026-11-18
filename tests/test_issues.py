"""
UIMRS Task 2 — Issue Management Security & Immutability Test Suite.

Verifies:
1. Secure citizen issue submission (JWT identity derivation).
2. Officer department-level authorization locks.
3. Audit trail creation.
4. Historical record immutability (Bengaluru logs).
5. Duplicate detection safety (no modification of historical data).

Runs against an ISOLATED in-memory SQLite database with StaticPool for concurrency.
"""
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# 1. Path configuration: Ensure backend is importable
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models, schemas
from app.database import Base, get_db
from app.main import app
from app.routers.auth import create_access_token, hash_password
from app.services.classification_service import ClassificationResult, PriorityResult
from app.services.image_verification_service import ImageCheckResult

# Isolated Test Database Setup
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


class TestIssuesSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Create tables once
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)

    def setUp(self):
        # Clear tables between tests
        self.db = TestingSessionLocal()
        # Dependency overrides
        self.old_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_db] = override_get_db
        
        # Clear data in correct order (FK constraints)
        self.db.query(models.IssueStatusUpdate).delete()
        self.db.query(models.IssueImage).delete()
        self.db.query(models.Issue).delete()
        self.db.query(models.User).delete()
        self.db.query(models.Department).delete()
        self.db.commit()

        # Create baseline test fixtures
        self._setup_fixtures()

    def tearDown(self):
        self.db.close()
        app.dependency_overrides = self.old_overrides

    def _setup_fixtures(self):
        # TEST FIXTURES: Departments (Removed city as per user hint)
        self.dept_roads = models.Department(
            name="TEST_DEPT_ROADS",
            handles_category=models.IssueCategory.ROAD_DAMAGE
        )
        self.dept_garbage = models.Department(
            name="TEST_DEPT_GARBAGE",
            handles_category=models.IssueCategory.GARBAGE_WASTE
        )
        self.db.add_all([self.dept_roads, self.dept_garbage])
        self.db.commit()

        # TEST FIXTURES: Users
        self.citizen_a = models.User(
            name="TEST_CITIZEN_A",
            email="citizen.a@uimrs.test",
            password_hash=hash_password("Pass123!"),
            role=models.UserRole.CITIZEN
        )
        self.citizen_b = models.User(
            name="TEST_CITIZEN_B",
            email="citizen.b@uimrs.test",
            password_hash=hash_password("Pass123!"),
            role=models.UserRole.CITIZEN
        )
        self.officer_roads = models.User(
            name="TEST_OFFICER_ROADS",
            email="officer.roads@uimrs.test",
            password_hash=hash_password("Pass123!"),
            role=models.UserRole.OFFICER,
            department_id=self.dept_roads.id
        )
        self.officer_no_dept = models.User(
            name="TEST_OFFICER_NO_DEPT",
            email="officer.none@uimrs.test",
            password_hash=hash_password("Pass123!"),
            role=models.UserRole.OFFICER,
            department_id=None
        )
        self.admin = models.User(
            name="TEST_ADMIN",
            email="admin@uimrs.test",
            password_hash=hash_password("Pass123!"),
            role=models.UserRole.ADMIN
        )
        self.db.add_all([self.citizen_a, self.citizen_b, self.officer_roads, self.officer_no_dept, self.admin])
        self.db.commit()

        # Tokens
        self.token_a = create_access_token(self.citizen_a)
        self.token_roads = create_access_token(self.officer_roads)
        self.token_admin = create_access_token(self.admin)
        self.token_none = create_access_token(self.officer_no_dept)

    # --- A. Citizen Submission Tests ---

    @patch("app.routers.issues.classifier.classify")
    @patch("app.routers.issues.priority_engine.score")
    def test_citizen_submit_valid(self, mock_priority, mock_classify):
        mock_classify.return_value = ClassificationResult(
            category=models.IssueCategory.ROAD_DAMAGE,
            confidence=0.9,
            source="ml_model"
        )
        mock_priority.return_value = PriorityResult(score=5.0, level="Medium", reasons=["Test"])

        payload = {
            "title": "Large Pothole near Sector 29",
            "description": "A very deep and dangerous pothole on the main road.",
            "latitude": 28.4601,
            "longitude": 77.0648,
            "city": "Gurugram, Haryana, India"
        }
        headers = {"Authorization": f"Bearer {self.token_a}"}
        response = self.client.post("/api/v1/issues", json=payload, headers=headers)
        
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["title"], payload["title"])
        
        # Verify identity derivation in DB
        issue = self.db.query(models.Issue).filter(models.Issue.id == data["id"]).first()
        self.assertEqual(issue.reporter_id, self.citizen_a.id)

    def test_anonymous_submit_rejected(self):
        payload = {
            "title": "Anonymous Report Title",
            "description": "This should fail because of no token.",
            "latitude": 28.4601,
            "longitude": 77.0648,
            "city": "Gurugram, Haryana, India"
        }
        response = self.client.post("/api/v1/issues", json=payload)
        self.assertEqual(response.status_code, 401)

    @patch("app.routers.issues.classifier.classify")
    @patch("app.routers.issues.priority_engine.score")
    def test_citizen_forged_reporter_id_ignored(self, mock_priority, mock_classify):
        mock_classify.return_value = ClassificationResult(
            category=models.IssueCategory.ROAD_DAMAGE, confidence=0.9, source="test"
        )
        mock_priority.return_value = PriorityResult(score=5.0, level="Medium", reasons=[])

        payload = {
            "title": "Forged ID Test",
            "description": "Attempting to impersonate Citizen B.",
            "latitude": 28.4601,
            "longitude": 77.0648,
            "city": "Gurugram, Haryana, India",
            "reporter_id": self.citizen_b.id  # FORGED
        }
        headers = {"Authorization": f"Bearer {self.token_a}"}
        response = self.client.post("/api/v1/issues", json=payload, headers=headers)
        
        self.assertEqual(response.status_code, 201)
        data = response.json()
        
        # Verify the forged ID was ignored or rejected (it should be ignored as per schemas.py)
        issue = self.db.query(models.Issue).filter(models.Issue.id == data["id"]).first()
        self.assertEqual(issue.reporter_id, self.citizen_a.id)

    # --- B. Officer Permission Tests ---

    def test_citizen_cannot_update_status(self):
        # Create a live issue
        issue = models.Issue(
            ticket_number="TEST-2026-101",
            title="Live Issue",
            description="Test description for status update",
            category=models.IssueCategory.ROAD_DAMAGE,
            status=models.IssueStatus.REPORTED,
            latitude=28.4, longitude=77.0,
            reporter_id=self.citizen_a.id,
            department_id=self.dept_roads.id
        )
        self.db.add(issue)
        self.db.commit()

        headers = {"Authorization": f"Bearer {self.token_a}"}
        response = self.client.patch(f"/api/v1/issues/{issue.id}/status", 
                                    json={"new_status": "in_progress"}, headers=headers)
        self.assertEqual(response.status_code, 403)

    def test_officer_same_department_allowed(self):
        issue = models.Issue(
            ticket_number="TEST-2026-102",
            title="Road Issue",
            description="Test road issue",
            category=models.IssueCategory.ROAD_DAMAGE,
            status=models.IssueStatus.REPORTED,
            latitude=28.4, longitude=77.0,
            reporter_id=self.citizen_a.id,
            department_id=self.dept_roads.id,
            assigned_officer_id=self.officer_roads.id,
            routing_status="assigned",
        )
        self.db.add(issue)
        self.db.commit()

        headers = {"Authorization": f"Bearer {self.token_roads}"}
        response = self.client.patch(f"/api/v1/issues/{issue.id}/status", 
                                    json={"new_status": "in_progress", "comment": "Starting fix"}, headers=headers)
        self.assertEqual(response.status_code, 200)
        
        # Verify audit trail
        audit = self.db.query(models.IssueStatusUpdate).filter(models.IssueStatusUpdate.issue_id == issue.id).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.updated_by_id, self.officer_roads.id)
        self.assertEqual(audit.new_status, models.IssueStatus.IN_PROGRESS)

    def test_officer_different_department_rejected(self):
        issue = models.Issue(
            ticket_number="TEST-2026-103",
            title="Garbage Issue",
            description="Test garbage issue",
            category=models.IssueCategory.GARBAGE_WASTE,
            status=models.IssueStatus.REPORTED,
            latitude=28.4, longitude=77.0,
            reporter_id=self.citizen_a.id,
            department_id=self.dept_garbage.id
        )
        self.db.add(issue)
        self.db.commit()

        headers = {"Authorization": f"Bearer {self.token_roads}"} # Road officer vs Garbage issue
        response = self.client.patch(f"/api/v1/issues/{issue.id}/status", 
                                    json={"new_status": "acknowledged"}, headers=headers)
        self.assertEqual(response.status_code, 403)
        self.assertIn("not authorized", response.json()["detail"])

    def test_officer_no_department_rejected(self):
        issue = models.Issue(
            ticket_number="TEST-2026-104",
            title="Unassigned Issue",
            description="No department assigned yet",
            category=models.IssueCategory.ROAD_DAMAGE,
            status=models.IssueStatus.REPORTED,
            latitude=28.4, longitude=77.0,
            reporter_id=self.citizen_a.id,
            department_id=None
        )
        self.db.add(issue)
        self.db.commit()

        headers = {"Authorization": f"Bearer {self.token_none}"}
        response = self.client.patch(f"/api/v1/issues/{issue.id}/status", 
                                    json={"new_status": "acknowledged"}, headers=headers)
        self.assertEqual(response.status_code, 403)
        self.assertIn("no assigned department", response.json()["detail"])

    def test_admin_bypass_allowed(self):
        issue = models.Issue(
            ticket_number="TEST-2026-105",
            title="Any Issue",
            description="Admin should bypass dept lock",
            category=models.IssueCategory.GARBAGE_WASTE,
            status=models.IssueStatus.REPORTED,
            latitude=28.4, longitude=77.0,
            reporter_id=self.citizen_a.id,
            department_id=self.dept_garbage.id
        )
        self.db.add(issue)
        self.db.commit()

        headers = {"Authorization": f"Bearer {self.token_admin}"}
        response = self.client.patch(f"/api/v1/issues/{issue.id}/status", 
                                    json={"new_status": "resolved", "comment": "Administrative resolution recorded."}, headers=headers)
        self.assertEqual(response.status_code, 200)

    # --- C. Historical Protection Tests ---

    def test_historical_immutability_status(self):
        # TEST FIXTURE: Historical Bengaluru record
        hist_issue = models.Issue(
            ticket_number="HIST-BLR-001",
            title="Historical Pothole",
            description="Janaagraha record from 2024",
            category=models.IssueCategory.ROAD_DAMAGE,
            status=models.IssueStatus.RESOLVED,
            latitude=12.9716, longitude=77.5946,
            reporter_id=self.citizen_a.id,
            is_historical=True,
            source_record_id="OLD-123",
            source_dataset="blr_civic_2024"
        )
        self.db.add(hist_issue)
        self.db.commit()

        headers = {"Authorization": f"Bearer {self.token_admin}"}
        response = self.client.patch(f"/api/v1/issues/{hist_issue.id}/status", 
                                    json={"new_status": "reported"}, headers=headers)
        self.assertEqual(response.status_code, 403)
        self.assertIn("immutable", response.json()["detail"])
        
        # Verify no changes in DB
        self.db.refresh(hist_issue)
        self.assertEqual(hist_issue.status, models.IssueStatus.RESOLVED)
        self.assertEqual(hist_issue.source_record_id, "OLD-123")

    def test_historical_immutability_images(self):
        hist_issue = models.Issue(
            ticket_number="HIST-BLR-002",
            title="Historical Garbage",
            description="Test fixture",
            category=models.IssueCategory.GARBAGE_WASTE,
            status=models.IssueStatus.RESOLVED,
            latitude=12.97, longitude=77.59,
            reporter_id=self.citizen_a.id,
            is_historical=True
        )
        self.db.add(hist_issue)
        self.db.commit()

        headers = {"Authorization": f"Bearer {self.token_a}"}
        # Fake file upload
        files = {"file": ("test.jpg", b"fake-content", "image/jpeg")}
        response = self.client.post(f"/api/v1/issues/{hist_issue.id}/images", files=files, headers=headers)
        
        self.assertEqual(response.status_code, 403)
        self.assertIn("historical", response.json()["detail"])

    @patch("app.routers.issues.classifier.classify")
    @patch("app.routers.issues.priority_engine.score")
    def test_duplicate_detection_skips_historical(self, mock_priority, mock_classify):
        # 1. Setup a historical issue in Bengaluru
        hist_issue = models.Issue(
            ticket_number="HIST-BLR-100",
            title="Historical Road Damage",
            description="Test Fixture - Historical Bengaluru Log",
            category=models.IssueCategory.ROAD_DAMAGE,
            status=models.IssueStatus.REPORTED,
            latitude=12.9716, longitude=77.5946,
            reporter_id=self.citizen_a.id,
            is_historical=True,
            duplicate_count=0,
            source_record_id="OLD-456",
            source_dataset="blr_test_fixture"
        )
        self.db.add(hist_issue)
        self.db.commit()

        # 2. Submit a new live issue at the EXACT same location
        mock_classify.return_value = ClassificationResult(
            category=models.IssueCategory.ROAD_DAMAGE, confidence=1.0, source="test"
        )
        mock_priority.return_value = PriorityResult(score=2.0, level="Low", reasons=[])

        payload = {
            "title": "New Pothole Report",
            "description": "Reporting the same spot as the historical record.",
            "latitude": 12.9716,
            "longitude": 77.5946,
            "city": "Bengaluru, Karnataka, India"
        }
        headers = {"Authorization": f"Bearer {self.token_a}"}
        response = self.client.post("/api/v1/issues", json=payload, headers=headers)
        
        self.assertEqual(response.status_code, 201)
        data = response.json()
        
        # New issue must NOT be marked as duplicate of the historical one
        self.assertEqual(data["status"], "reported")
        
        # Historical record must remain UNCHANGED
        self.db.refresh(hist_issue)
        self.assertEqual(hist_issue.duplicate_count, 0)
        self.assertEqual(hist_issue.title, "Historical Road Damage")
        self.assertEqual(hist_issue.source_record_id, "OLD-456")

if __name__ == "__main__":
    unittest.main()
