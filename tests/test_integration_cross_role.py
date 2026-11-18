"""UIMRS Task 11B — cross-role integration test.

Exercises a genuine end-to-end workflow against an isolated temporary SQLite
database:

citizen registration/login -> complaint submission -> automatic routing ->
officer claim and status update -> administrator review and audited action.

No operational database or protected file is touched.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.core.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.routers.auth import create_access_token, hash_password
from app.services.classification_service import CITY_GURUGRAM as SERVICE_CITY_GURUGRAM
from app.services.department_routing import ROUTING_MODE_AUTOMATIC

TEST_UPLOAD_DIR = None


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


class TestCrossRoleIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=cls.engine)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)
        cls.client = TestClient(app)

    def setUp(self):
        self.db = self.SessionLocal()
        self.old_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_db] = self._override_get_db

        self.original_upload_dir = get_settings().UPLOAD_DIR
        self.upload_dir = tempfile.mkdtemp(prefix="uimrs_cross_role_")
        get_settings().UPLOAD_DIR = self.upload_dir

        for model in (
            models.AdminAuditLog,
            models.IssueStatusUpdate,
            models.IssueImage,
            models.Issue,
            models.User,
            models.Department,
        ):
            self.db.query(model).delete()
        self.db.commit()

        self.admin_password = "AdminCrossRole#2026"
        self.officer_password = "OfficerCrossRole#2026"
        self.citizen_password = "CitizenCrossRole#2026"

        self.road_department = models.Department(
            name="CROSS ROLE ROADS",
            handles_category=models.IssueCategory.ROAD_DAMAGE,
            city=SERVICE_CITY_GURUGRAM,
            source_agency="MCG",
            routing_mode=ROUTING_MODE_AUTOMATIC,
            verification_source_url="https://mcg.gov.in/contact/office-contacts",
            verification_date="2026-11-14",
            is_active=True,
        )
        self.garbage_department = models.Department(
            name="CROSS ROLE GARBAGE",
            handles_category=models.IssueCategory.GARBAGE_WASTE,
            city=SERVICE_CITY_GURUGRAM,
            source_agency="MCG",
            routing_mode="manual_review",
            verification_source_url="https://mcg.gov.in/contact/office-contacts",
            verification_date="2026-11-14",
            is_active=True,
        )
        self.db.add_all([self.road_department, self.garbage_department])
        self.db.commit()

        self.admin = models.User(
            name="Cross Role Administrator",
            email="cross.admin@example.com",
            password_hash=hash_password(self.admin_password),
            role=models.UserRole.ADMIN,
        )
        self.db.add(self.admin)
        self.db.commit()

    def _override_get_db(self):
        db = self.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def tearDown(self):
        self.db.close()
        app.dependency_overrides = self.old_overrides
        get_settings().UPLOAD_DIR = self.original_upload_dir
        shutil.rmtree(self.upload_dir, ignore_errors=True)

    def _login(self, email, password):
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    def test_cross_role_workflow(self):
        admin_token = self._login("cross.admin@example.com", self.admin_password)

        # Provision a real officer account through the administrator API.
        officer_email = "cross.officer@example.com"
        create_officer = self.client.post(
            "/api/v1/admin/officers",
            headers=bearer(admin_token),
            json={
                "name": "Cross Role Officer",
                "email": officer_email,
                "password": self.officer_password,
                "department_id": self.road_department.id,
                "preferred_language": "en",
            },
        )
        self.assertEqual(create_officer.status_code, 201, create_officer.text)
        self.assertEqual(create_officer.json()["role"], "officer")

        # Citizen registers through the public endpoint and logs in.
        register = self.client.post(
            "/api/v1/auth/register",
            json={
                "name": "Cross Role Citizen",
                "email": "cross.citizen@example.com",
                "password": self.citizen_password,
            },
        )
        self.assertEqual(register.status_code, 201, register.text)
        self.assertEqual(register.json()["role"], "citizen")
        citizen_token = self._login("cross.citizen@example.com", self.citizen_password)

        # Citizen submits a complaint that should classify as road damage and
        # route automatically to the verified roads department.
        road_issue = self.client.post(
            "/api/v1/issues",
            headers=bearer(citizen_token),
            json={
                "title": "Large pothole on the main road",
                "description": "A deep pothole on the road near the market is causing accidents.",
                "city": "Gurugram, Haryana, India",
                "latitude": 28.4595,
                "longitude": 77.0266,
                "address_text": "Market road, Gurugram",
            },
        )
        self.assertEqual(road_issue.status_code, 201, road_issue.text)
        road_issue_id = road_issue.json()["id"]
        self.assertEqual(road_issue.json()["routing_status"], "assigned")
        self.assertEqual(road_issue.json()["department_id"], self.road_department.id)

        # A second complaint lands in manual review so an administrator can make
        # a documented routing decision.
        manual_issue = self.client.post(
            "/api/v1/issues",
            headers=bearer(citizen_token),
            json={
                "title": "Garbage pile near the market",
                "description": "A large garbage pile and waste dumping near the market.",
                "city": "Gurugram, Haryana, India",
                "latitude": 28.4601,
                "longitude": 77.0271,
                "address_text": "Market area, Gurugram",
            },
        )
        self.assertEqual(manual_issue.status_code, 201, manual_issue.text)
        manual_issue_id = manual_issue.json()["id"]
        self.assertEqual(manual_issue.json()["routing_status"], "unassigned_review")

        officer_token = self._login(officer_email, self.officer_password)

        # Officer claims the automatically routed complaint.
        claim = self.client.post(
            f"/api/v1/officer/issues/{road_issue_id}/claim",
            headers=bearer(officer_token),
        )
        self.assertEqual(claim.status_code, 200, claim.text)
        self.assertTrue(claim.json()["claimed"])

        # Officer updates status with a mandatory comment.
        update = self.client.patch(
            f"/api/v1/issues/{road_issue_id}/status",
            headers=bearer(officer_token),
            json={"new_status": "in_progress", "comment": "Crew dispatched to repair the pothole."},
        )
        self.assertEqual(update.status_code, 200, update.text)
        self.assertEqual(update.json()["status"], "in_progress")

        # Administrator reviews the complaint detail.
        detail = self.client.get(
            f"/api/v1/admin/issues/{road_issue_id}",
            headers=bearer(admin_token),
        )
        self.assertEqual(detail.status_code, 200, detail.text)

        # Administrator makes a documented routing decision on the manual-review complaint.
        route = self.client.post(
            f"/api/v1/admin/issues/{manual_issue_id}/route",
            headers=bearer(admin_token),
            json={
                "department_id": self.garbage_department.id,
                "reason": "Verified municipal sanitation ownership for this market area.",
            },
        )
        self.assertEqual(route.status_code, 200, route.text)
        self.assertEqual(route.json()["action"], "issue.routed")

        # The administrative action is present in the audit log.
        audit = self.client.get(
            "/api/v1/admin/audit-logs",
            headers=bearer(admin_token),
        )
        self.assertEqual(audit.status_code, 200, audit.text)
        actions = {entry["action"] for entry in audit.json()["items"]}
        self.assertIn("issue.routed", actions)
        self.assertIn("officer.created", actions)

        overview = self.client.get(
            "/api/v1/admin/overview",
            headers=bearer(admin_token),
        )
        self.assertEqual(overview.status_code, 200, overview.text)
        self.assertGreaterEqual(overview.json()["live"]["total"], 2)


if __name__ == "__main__":
    unittest.main()
