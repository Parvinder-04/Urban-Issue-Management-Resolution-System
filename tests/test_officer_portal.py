"""UIMRS Task 9 — department-specific officer portal and complaint workflow.

Covers:
A. Controlled officer provisioning and authentication.
B. Live, exact-department, exact-city, verified-routing queue scoping.
C. Atomic claiming and concurrent-claim conflict handling.
D. Claim-before-update, ownership, mandatory comments, transitions and audit.
E. Department dashboard counts derived from real scoped queries.
F. Authenticated evidence access and historical restrictions.
G. Officer portal route, API contracts and safe DOM rendering.

All API tests use an isolated in-memory SQLite database and a temporary upload
directory. The concurrency test uses a temporary file database. The real
project databases are never opened or written by this module.
"""
import importlib.util
import re
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
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
from app.routers.auth import create_access_token, hash_password, verify_password
from app.services.classification_service import CITY_BENGALURU, CITY_GURUGRAM
from app.services.department_routing import (
    ROUTING_MODE_AUTOMATIC,
    ROUTING_STATUS_ASSIGNED,
    ROUTING_STATUS_UNASSIGNED_NO_MAPPING,
    ROUTING_STATUS_UNASSIGNED_REVIEW,
)
from app.services.officer_provisioning import (
    OfficerProvisioningError,
    provision_officer,
)
from app.services.officer_workflow import (
    IssueAlreadyClaimedError,
    claim_issue,
)

TEST_PASSWORD = "OfficerPortal#2026"
PORTAL_PAGE = PROJECT_ROOT / "frontend" / "officer_portal.html"
CITIZEN_PAGE = PROJECT_ROOT / "frontend" / "citizen_portal.html"
HISTORICAL_PAGE = PROJECT_ROOT / "frontend" / "index_india_staging.html"
VERIFIED_SOURCE = "https://mcg.gov.in/contact/office-contacts"
VERIFIED_DATE = "2026-11-14"

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


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def make_automatic_department(name, category, city):
    return models.Department(
        name=name,
        handles_category=category,
        city=city,
        source_agency=name,
        routing_mode=ROUTING_MODE_AUTOMATIC,
        verification_source_url=VERIFIED_SOURCE,
        verification_date=VERIFIED_DATE,
        is_active=True,
    )


class OfficerPortalTestBase(unittest.TestCase):
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
        self.upload_dir = tempfile.mkdtemp(prefix="uimrs_officer_evidence_")
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
            "title": f"Fixture {ticket}",
            "description": "Isolated Task 9 fixture description, not a real complaint.",
            "category": models.IssueCategory.ROAD_DAMAGE,
            "status": models.IssueStatus.REPORTED,
            "latitude": 28.4595,
            "longitude": 77.0266,
            "city": CITY_GURUGRAM,
            "reporter_id": reporter.id,
            "department_id": department.id if department else None,
            "is_historical": False,
            "routing_status": ROUTING_STATUS_ASSIGNED,
            "priority_score": 1.0,
            "priority_level": models.PriorityLevel.LOW,
        }
        values.update(overrides)
        issue = models.Issue(ticket_number=ticket, **values)
        self.db.add(issue)
        self.db.commit()
        self.db.refresh(issue)
        return issue

    def _write_evidence(self, filename, content=b"isolated-test-evidence"):
        evidence_dir = Path(self.upload_dir) / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        path = evidence_dir / filename
        path.write_bytes(content)
        return f"evidence/{filename}"

    def _image(self, issue, filename, **overrides):
        values = {
            "issue_id": issue.id,
            "image_url": self._write_evidence(filename),
            "passed_verification": True,
            "blur_score": 1.0,
            "brightness": 120.0,
            "resolution_width": 400,
            "resolution_height": 300,
        }
        values.update(overrides)
        image = models.IssueImage(**values)
        self.db.add(image)
        self.db.commit()
        self.db.refresh(image)
        return image

    def _setup_fixtures(self):
        self.dept_roads = make_automatic_department(
            "TEST_OFFICER_GURUGRAM_ROADS",
            models.IssueCategory.ROAD_DAMAGE,
            CITY_GURUGRAM,
        )
        self.dept_garbage = make_automatic_department(
            "TEST_OFFICER_GURUGRAM_GARBAGE",
            models.IssueCategory.GARBAGE_WASTE,
            CITY_GURUGRAM,
        )
        self.dept_bengaluru = make_automatic_department(
            "TEST_OFFICER_BENGALURU_ROADS",
            models.IssueCategory.ROAD_DAMAGE,
            CITY_BENGALURU,
        )
        self.dept_review = models.Department(
            name="TEST_OFFICER_REVIEW_STREETLIGHT",
            handles_category=models.IssueCategory.STREETLIGHT,
            city=CITY_GURUGRAM,
            source_agency="TEST_OFFICER_REVIEW",
            routing_mode="manual_review",
            verification_source_url=VERIFIED_SOURCE,
            verification_date=VERIFIED_DATE,
            is_active=True,
        )
        self.dept_inactive = models.Department(
            name="TEST_OFFICER_INACTIVE_WATER",
            handles_category=models.IssueCategory.WATER_SUPPLY,
            city=CITY_GURUGRAM,
            source_agency="TEST_OFFICER_INACTIVE",
            routing_mode=ROUTING_MODE_AUTOMATIC,
            verification_source_url=VERIFIED_SOURCE,
            verification_date=VERIFIED_DATE,
            is_active=False,
        )
        self.db.add_all([
            self.dept_roads, self.dept_garbage, self.dept_bengaluru,
            self.dept_review, self.dept_inactive,
        ])
        self.db.commit()

        self.citizen_a = self._user(
            "TEST OFFICER CITIZEN A", "task9.citizen.a@example.com",
            models.UserRole.CITIZEN,
        )
        self.citizen_b = self._user(
            "TEST OFFICER CITIZEN B", "task9.citizen.b@example.com",
            models.UserRole.CITIZEN,
        )
        self.officer_roads = self._user(
            "TEST OFFICER ROADS", "task9.officer.roads@example.com",
            models.UserRole.OFFICER, self.dept_roads.id,
        )
        self.officer_roads_2 = self._user(
            "TEST OFFICER ROADS TWO", "task9.officer.roads.two@example.com",
            models.UserRole.OFFICER, self.dept_roads.id,
        )
        self.officer_garbage = self._user(
            "TEST OFFICER GARBAGE", "task9.officer.garbage@example.com",
            models.UserRole.OFFICER, self.dept_garbage.id,
        )
        self.officer_bengaluru = self._user(
            "TEST OFFICER BENGALURU", "task9.officer.bengaluru@example.com",
            models.UserRole.OFFICER, self.dept_bengaluru.id,
        )
        self.officer_no_department = self._user(
            "TEST OFFICER NO DEPARTMENT", "task9.officer.none@example.com",
            models.UserRole.OFFICER, None,
        )
        self.admin = self._user(
            "TEST OFFICER ADMIN", "task9.admin@example.com",
            models.UserRole.ADMIN,
        )
        self.db.add_all([
            self.citizen_a, self.citizen_b, self.officer_roads,
            self.officer_roads_2, self.officer_garbage,
            self.officer_bengaluru, self.officer_no_department, self.admin,
        ])
        self.db.commit()

        self.token_a = create_access_token(self.citizen_a)
        self.token_b = create_access_token(self.citizen_b)
        self.token_roads = create_access_token(self.officer_roads)
        self.token_roads_2 = create_access_token(self.officer_roads_2)
        self.token_garbage = create_access_token(self.officer_garbage)
        self.token_bengaluru = create_access_token(self.officer_bengaluru)
        self.token_none = create_access_token(self.officer_no_department)
        self.token_admin = create_access_token(self.admin)

        now = datetime.now(timezone.utc)
        self.eligible_unclaimed = self._issue(
            "TASK9-ELIGIBLE-1", self.citizen_a, self.dept_roads,
            title="TEST OFFICER ELIGIBLE POTHOLE",
            description="TEST OFFICER ELIGIBLE DESCRIPTION near the market.",
            priority_score=8.0, priority_level=models.PriorityLevel.HIGH,
        )
        self.eligible_ack = self._issue(
            "TASK9-ELIGIBLE-2", self.citizen_b, self.dept_roads,
            title="TEST OFFICER ACKNOWLEDGED ROAD",
            status=models.IssueStatus.ACKNOWLEDGED,
            priority_score=4.0, priority_level=models.PriorityLevel.MEDIUM,
        )
        self.claimed_by_me = self._issue(
            "TASK9-MINE-1", self.citizen_a, self.dept_roads,
            title="TEST OFFICER IN PROGRESS ROAD",
            status=models.IssueStatus.IN_PROGRESS,
            assigned_officer_id=self.officer_roads.id,
            priority_score=9.0, priority_level=models.PriorityLevel.CRITICAL,
        )
        self.claimed_by_other = self._issue(
            "TASK9-OTHER-1", self.citizen_b, self.dept_roads,
            title="TEST OFFICER OTHER OFFICER ROAD",
            assigned_officer_id=self.officer_roads_2.id,
        )
        self.resolved_mine = self._issue(
            "TASK9-RESOLVED-1", self.citizen_a, self.dept_roads,
            title="TEST OFFICER RESOLVED ROAD",
            status=models.IssueStatus.RESOLVED,
            assigned_officer_id=self.officer_roads.id,
            resolved_at=now,
        )
        self.resolved_unclaimed = self._issue(
            "TASK9-RESOLVED-2", self.citizen_b, self.dept_roads,
            title="TEST OFFICER RESOLVED UNCLAIMED",
            status=models.IssueStatus.RESOLVED,
            resolved_at=now,
        )
        self.assigned_reported = self._issue(
            "TASK9-ASSIGNED-1", self.citizen_a, self.dept_roads,
            title="TEST OFFICER ASSIGNED REPORTED ROAD",
            assigned_officer_id=self.officer_roads.id,
            priority_score=7.0, priority_level=models.PriorityLevel.HIGH,
        )
        self.other_department = self._issue(
            "TASK9-DEPT-1", self.citizen_a, self.dept_garbage,
            title="TEST OFFICER OTHER DEPARTMENT GARBAGE",
            category=models.IssueCategory.GARBAGE_WASTE,
        )
        self.other_city = self._issue(
            "TASK9-CITY-1", self.citizen_b, self.dept_bengaluru,
            title="TEST OFFICER OTHER CITY ROAD",
            city=CITY_BENGALURU, latitude=12.9716, longitude=77.5946,
        )
        self.historical = self._issue(
            "TASK9-HIST-1", self.admin, self.dept_roads,
            title="TEST OFFICER HISTORICAL ROAD",
            status=models.IssueStatus.RESOLVED,
            is_historical=True,
            routing_status=None,
            source_record_id="TASK9-HIST-SOURCE-1",
        )
        self.unassigned_review = self._issue(
            "TASK9-REVIEW-1", self.citizen_a, self.dept_roads,
            title="TEST OFFICER REVIEW ROAD",
            routing_status=ROUTING_STATUS_UNASSIGNED_REVIEW,
        )
        self.unassigned_no_mapping = self._issue(
            "TASK9-NOMAP-1", self.citizen_b, None,
            title="TEST OFFICER NO MAPPING ROAD",
            routing_status=ROUTING_STATUS_UNASSIGNED_NO_MAPPING,
        )
        self.city_mismatch = self._issue(
            "TASK9-MISMATCH-1", self.citizen_a, self.dept_roads,
            title="TEST OFFICER CITY MISMATCH ROAD",
            city=CITY_BENGALURU, latitude=12.9716, longitude=77.5946,
        )
        self.review_assigned = self._issue(
            "TASK9-REVIEW-2", self.citizen_b, self.dept_roads,
            title="TEST OFFICER REVIEW ASSIGNED ROAD",
            routing_status=ROUTING_STATUS_UNASSIGNED_REVIEW,
            assigned_officer_id=self.officer_roads.id,
        )
        self.cross_city_assigned = self._issue(
            "TASK9-CROSS-CITY-1", self.citizen_a, self.dept_roads,
            title="TEST OFFICER CROSS CITY ASSIGNED ROAD",
            city=CITY_BENGALURU, latitude=12.9716, longitude=77.5946,
            assigned_officer_id=self.officer_roads.id,
        )

        self.image_eligible = self._image(
            self.eligible_unclaimed, "task9-eligible.jpg"
        )
        self.image_other = self._image(
            self.other_department, "task9-other.jpg"
        )
        self.image_historical = self._image(
            self.historical, "task9-historical.jpg"
        )

        self.db.add_all([
            models.IssueStatusUpdate(
                issue_id=self.claimed_by_me.id,
                updated_by_id=self.officer_roads.id,
                old_status=models.IssueStatus.REPORTED,
                new_status=models.IssueStatus.IN_PROGRESS,
                comment="Crew dispatched to inspect the road.",
                created_at=now - timedelta(hours=2),
            ),
            models.IssueStatusUpdate(
                issue_id=self.resolved_mine.id,
                updated_by_id=self.officer_roads.id,
                old_status=models.IssueStatus.IN_PROGRESS,
                new_status=models.IssueStatus.RESOLVED,
                comment="Repair completed and inspected.",
                created_at=now - timedelta(hours=1),
            ),
        ])
        self.db.commit()

    def _ids(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return {row["id"] for row in response.json()}


# ---------------------------------------------------------------------------
# A. Provisioning and authentication
# ---------------------------------------------------------------------------
class TestOfficerProvisioningAndAuthentication(OfficerPortalTestBase):
    def test_provisioning_forces_officer_role_and_hashes_password(self):
        provisioned = provision_officer(
            self.db,
            name="Provisioned Officer",
            email="task9.provisioned@example.com",
            phone="+91 90000 09001",
            department_id=self.dept_roads.id,
            password="Provisioned#2026",
        )
        self.assertEqual(provisioned.role, "officer")
        self.assertEqual(provisioned.department_id, self.dept_roads.id)

        user = self.db.query(models.User).filter(
            models.User.email == "task9.provisioned@example.com"
        ).first()
        self.assertIsNotNone(user)
        self.assertEqual(user.role, models.UserRole.OFFICER)
        self.assertEqual(user.department_id, self.dept_roads.id)
        self.assertNotEqual(user.password_hash, "Provisioned#2026")
        self.assertNotIn("Provisioned#2026", user.password_hash)
        self.assertTrue(verify_password("Provisioned#2026", user.password_hash))

    def test_duplicate_email_is_rejected_without_modifying_existing_user(self):
        before = self.db.query(models.User).filter(
            models.User.id == self.officer_roads.id
        ).first()
        original_hash = before.password_hash

        with self.assertRaises(OfficerProvisioningError):
            provision_officer(
                self.db,
                name="Duplicate Officer",
                email=self.officer_roads.email,
                department_id=self.dept_roads.id,
                password="Duplicate#2026",
            )

        self.db.expire_all()
        after = self.db.query(models.User).filter(
            models.User.id == self.officer_roads.id
        ).first()
        self.assertEqual(after.password_hash, original_hash)
        self.assertEqual(after.role, models.UserRole.OFFICER)
        count = self.db.query(models.User).filter(
            models.User.email == self.officer_roads.email
        ).count()
        self.assertEqual(count, 1)

    def test_missing_inactive_and_unsupported_city_departments_rejected(self):
        with self.assertRaises(OfficerProvisioningError):
            provision_officer(
                self.db,
                name="Missing Department",
                email="task9.missing.dept@example.com",
                department_id="does-not-exist",
                password="MissingDept#2026",
            )
        with self.assertRaises(OfficerProvisioningError):
            provision_officer(
                self.db,
                name="Inactive Department",
                email="task9.inactive.dept@example.com",
                department_id=self.dept_inactive.id,
                password="InactiveDept#2026",
            )

        unsupported = models.Department(
            name="TEST OFFICER UNSUPPORTED CITY",
            handles_category=models.IssueCategory.OTHER,
            city="New York, USA",
            routing_mode="manual_review",
            is_active=True,
        )
        self.db.add(unsupported)
        self.db.commit()
        with self.assertRaises(OfficerProvisioningError):
            provision_officer(
                self.db,
                name="Unsupported City",
                email="task9.unsupported.city@example.com",
                department_id=unsupported.id,
                password="Unsupported#2026",
            )

    def test_short_password_and_empty_name_rejected(self):
        with self.assertRaises(OfficerProvisioningError):
            provision_officer(
                self.db,
                name="Short Password",
                email="task9.short.password@example.com",
                department_id=self.dept_roads.id,
                password="short",
            )
        with self.assertRaises(OfficerProvisioningError):
            provision_officer(
                self.db,
                name="   ",
                email="task9.empty.name@example.com",
                department_id=self.dept_roads.id,
                password="EmptyName#2026",
            )

    def test_public_registration_cannot_create_officer(self):
        response = self.client.post(
            "/api/v1/auth/register",
            json={
                "name": "Public Registration",
                "email": "task9.public.registration@example.com",
                "password": "PublicRegistration#2026",
                "role": "admin",
                "department_id": self.dept_roads.id,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["role"], "citizen")
        self.assertIsNone(body["department_id"])

        stored = self.db.query(models.User).filter(
            models.User.email == "task9.public.registration@example.com"
        ).first()
        self.assertEqual(stored.role, models.UserRole.CITIZEN)
        self.assertIsNone(stored.department_id)

    def test_wrong_credentials_invalid_and_expired_tokens_rejected(self):
        wrong = self.client.post(
            "/api/v1/auth/login",
            json={"email": self.officer_roads.email, "password": "WrongPassword#2026"},
        )
        self.assertEqual(wrong.status_code, 401)

        invalid = self.client.get(
            "/api/v1/auth/me", headers=bearer("not-a-valid-jwt")
        )
        self.assertEqual(invalid.status_code, 401)

        expired = create_access_token(
            self.officer_roads, expires_delta=timedelta(seconds=-5)
        )
        expired_response = self.client.get(
            "/api/v1/auth/me", headers=bearer(expired)
        )
        self.assertEqual(expired_response.status_code, 401)
        self.assertIn("expired", expired_response.json()["detail"].lower())

    def test_officer_identity_and_department_come_from_token_and_database(self):
        login = self.client.post(
            "/api/v1/auth/login",
            json={"email": self.officer_roads.email, "password": TEST_PASSWORD},
        )
        self.assertEqual(login.status_code, 200, login.text)
        token = login.json()["access_token"]
        self.assertEqual(login.json()["user"]["role"], "officer")

        me = self.client.get("/api/v1/auth/me", headers=bearer(token))
        self.assertEqual(me.status_code, 200, me.text)
        body = me.json()
        self.assertEqual(body["id"], self.officer_roads.id)
        self.assertEqual(body["role"], "officer")
        self.assertEqual(body["department_id"], self.dept_roads.id)

    def test_provisioning_cli_has_no_password_argument_and_uses_hidden_prompt(self):
        script_path = PROJECT_ROOT / "scripts" / "provision_officer.py"
        source = script_path.read_text(encoding="utf-8")
        self.assertIn("getpass.getpass", source)
        self.assertNotIn("--password", source)

        spec = importlib.util.spec_from_file_location(
            "task9_provision_officer_cli", script_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        parser = module.build_parser()
        destinations = {action.dest for action in parser._actions}
        option_strings = {
            option
            for action in parser._actions
            for option in action.option_strings
        }
        self.assertNotIn("password", destinations)
        self.assertFalse(
            any("password" in option.lower() for option in option_strings),
            "the CLI must not accept a password argument",
        )


# ---------------------------------------------------------------------------
# B. Queue scoping
# ---------------------------------------------------------------------------
class TestOfficerQueueScoping(OfficerPortalTestBase):
    def test_all_scope_is_live_exact_department_exact_city_and_routed(self):
        ids = self._ids(
            self.client.get(
                "/api/v1/officer/issues?scope=all&limit=200",
                headers=bearer(self.token_roads),
            )
        )
        self.assertEqual(
            ids,
            {
                self.eligible_unclaimed.id,
                self.eligible_ack.id,
                self.claimed_by_me.id,
                self.claimed_by_other.id,
                self.resolved_mine.id,
                self.resolved_unclaimed.id,
                self.assigned_reported.id,
            },
        )
        self.assertNotIn(self.historical.id, ids)
        self.assertNotIn(self.other_department.id, ids)
        self.assertNotIn(self.other_city.id, ids)
        self.assertNotIn(self.unassigned_review.id, ids)
        self.assertNotIn(self.unassigned_no_mapping.id, ids)
        self.assertNotIn(self.city_mismatch.id, ids)
        self.assertNotIn(self.review_assigned.id, ids)
        self.assertNotIn(self.cross_city_assigned.id, ids)

    def test_default_queue_excludes_closed_complaints(self):
        ids = self._ids(
            self.client.get(
                "/api/v1/officer/issues?limit=200",
                headers=bearer(self.token_roads),
            )
        )
        self.assertEqual(
            ids,
            {
                self.eligible_unclaimed.id,
                self.eligible_ack.id,
                self.claimed_by_me.id,
                self.claimed_by_other.id,
                self.assigned_reported.id,
            },
        )
        self.assertNotIn(self.resolved_mine.id, ids)
        self.assertNotIn(self.resolved_unclaimed.id, ids)

    def test_unclaimed_and_mine_scopes(self):
        unclaimed = self._ids(
            self.client.get(
                "/api/v1/officer/issues?scope=unclaimed&limit=200",
                headers=bearer(self.token_roads),
            )
        )
        self.assertEqual(
            unclaimed, {self.eligible_unclaimed.id, self.eligible_ack.id}
        )

        mine = self._ids(
            self.client.get(
                "/api/v1/officer/issues?scope=mine&limit=200",
                headers=bearer(self.token_roads),
            )
        )
        self.assertEqual(
            mine,
            {
                self.claimed_by_me.id,
                self.resolved_mine.id,
                self.assigned_reported.id,
            },
        )

    def test_filters_only_narrow_the_scope(self):
        by_status = self._ids(
            self.client.get(
                "/api/v1/officer/issues?scope=all&status=in_progress&limit=200",
                headers=bearer(self.token_roads),
            )
        )
        self.assertEqual(by_status, {self.claimed_by_me.id})

        by_category = self._ids(
            self.client.get(
                "/api/v1/officer/issues?scope=all&category=Garbage_Waste&limit=200",
                headers=bearer(self.token_roads),
            )
        )
        self.assertEqual(by_category, set())

        by_priority = self._ids(
            self.client.get(
                "/api/v1/officer/issues?scope=all&priority=High&limit=200",
                headers=bearer(self.token_roads),
            )
        )
        self.assertEqual(
            by_priority, {self.eligible_unclaimed.id, self.assigned_reported.id}
        )

        by_search = self._ids(
            self.client.get(
                "/api/v1/officer/issues?scope=all&q=ELIGIBLE%20POTHOLE&limit=200",
                headers=bearer(self.token_roads),
            )
        )
        self.assertEqual(by_search, {self.eligible_unclaimed.id})

    def test_query_parameters_cannot_widen_department_or_city(self):
        ids = self._ids(
            self.client.get(
                f"/api/v1/officer/issues?scope=all&limit=200"
                f"&department_id={self.dept_garbage.id}"
                f"&city={CITY_BENGALURU}",
                headers=bearer(self.token_roads),
            )
        )
        self.assertNotIn(self.other_department.id, ids)
        self.assertNotIn(self.other_city.id, ids)
        self.assertIn(self.eligible_unclaimed.id, ids)

    def test_queue_requires_an_officer_with_a_supported_department(self):
        anonymous = self.client.get("/api/v1/officer/issues")
        self.assertEqual(anonymous.status_code, 401)

        citizen = self.client.get(
            "/api/v1/officer/issues", headers=bearer(self.token_a)
        )
        self.assertEqual(citizen.status_code, 403)

        admin = self.client.get(
            "/api/v1/officer/issues", headers=bearer(self.token_admin)
        )
        self.assertEqual(admin.status_code, 403)

        no_department = self.client.get(
            "/api/v1/officer/issues", headers=bearer(self.token_none)
        )
        self.assertEqual(no_department.status_code, 403)
        self.assertIn("department", no_department.json()["detail"].lower())


# ---------------------------------------------------------------------------
# C. Claiming
# ---------------------------------------------------------------------------
class TestOfficerClaiming(OfficerPortalTestBase):
    def test_eligible_complaint_can_be_claimed(self):
        response = self.client.post(
            f"/api/v1/officer/issues/{self.eligible_unclaimed.id}/claim",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["claimed"])
        self.assertFalse(body["already_claimed_by_you"])
        self.assertEqual(body["issue"]["assigned_officer_id"], self.officer_roads.id)

        self.db.expire_all()
        stored = self.db.query(models.Issue).filter(
            models.Issue.id == self.eligible_unclaimed.id
        ).first()
        self.assertEqual(stored.assigned_officer_id, self.officer_roads.id)

    def test_same_officer_reclaim_is_idempotent(self):
        first = self.client.post(
            f"/api/v1/officer/issues/{self.eligible_unclaimed.id}/claim",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(first.status_code, 200, first.text)
        second = self.client.post(
            f"/api/v1/officer/issues/{self.eligible_unclaimed.id}/claim",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["already_claimed_by_you"])

    def test_second_officer_cannot_claim_an_owned_complaint(self):
        first = self.client.post(
            f"/api/v1/officer/issues/{self.eligible_unclaimed.id}/claim",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(first.status_code, 200, first.text)

        second = self.client.post(
            f"/api/v1/officer/issues/{self.eligible_unclaimed.id}/claim",
            headers=bearer(self.token_roads_2),
        )
        self.assertEqual(second.status_code, 409, second.text)
        self.assertIn("another officer", second.json()["detail"].lower())

        self.db.expire_all()
        stored = self.db.query(models.Issue).filter(
            models.Issue.id == self.eligible_unclaimed.id
        ).first()
        self.assertEqual(stored.assigned_officer_id, self.officer_roads.id)

    def test_cross_department_claim_rejected(self):
        response = self.client.post(
            f"/api/v1/officer/issues/{self.other_department.id}/claim",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(response.status_code, 404)

    def test_historical_claim_rejected(self):
        response = self.client.post(
            f"/api/v1/officer/issues/{self.historical.id}/claim",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(response.status_code, 404)

    def test_manual_review_and_missing_mapping_claims_rejected(self):
        for issue in (
            self.unassigned_review,
            self.unassigned_no_mapping,
            self.review_assigned,
        ):
            with self.subTest(ticket=issue.ticket_number):
                response = self.client.post(
                    f"/api/v1/officer/issues/{issue.id}/claim",
                    headers=bearer(self.token_roads),
                )
                self.assertEqual(response.status_code, 404)

    def test_city_mismatch_and_closed_claims_rejected(self):
        for issue in (self.city_mismatch, self.cross_city_assigned, self.resolved_unclaimed):
            with self.subTest(ticket=issue.ticket_number):
                response = self.client.post(
                    f"/api/v1/officer/issues/{issue.id}/claim",
                    headers=bearer(self.token_roads),
                )
                self.assertEqual(response.status_code, 404)


class TestConcurrentClaiming(unittest.TestCase):
    """Two officers racing for one complaint cannot overwrite ownership."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="uimrs_officer_claim_")
        self.database_path = Path(self.temp_dir) / "claims.db"
        self.engine = create_engine(
            f"sqlite:///{self.database_path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": 30},
            isolation_level="AUTOCOMMIT",
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine
        )

        db = self.Session()
        department = make_automatic_department(
            "TEST_OFFICER_CONCURRENT_ROADS",
            models.IssueCategory.ROAD_DAMAGE,
            CITY_GURUGRAM,
        )
        db.add(department)
        db.commit()

        password_hash = hash_password(TEST_PASSWORD)
        officer_one = models.User(
            name="Concurrent Officer One",
            email="task9.concurrent.one@example.com",
            password_hash=password_hash,
            role=models.UserRole.OFFICER,
            department_id=department.id,
        )
        officer_two = models.User(
            name="Concurrent Officer Two",
            email="task9.concurrent.two@example.com",
            password_hash=password_hash,
            role=models.UserRole.OFFICER,
            department_id=department.id,
        )
        citizen = models.User(
            name="Concurrent Citizen",
            email="task9.concurrent.citizen@example.com",
            password_hash=password_hash,
            role=models.UserRole.CITIZEN,
        )
        db.add_all([officer_one, officer_two, citizen])
        db.commit()

        issue = models.Issue(
            ticket_number="TASK9-CONCURRENT-1",
            title="Concurrent claim fixture",
            description="Isolated fixture used only for the claim race test.",
            category=models.IssueCategory.ROAD_DAMAGE,
            status=models.IssueStatus.REPORTED,
            latitude=28.4595,
            longitude=77.0266,
            city=CITY_GURUGRAM,
            reporter_id=citizen.id,
            department_id=department.id,
            is_historical=False,
            routing_status=ROUTING_STATUS_ASSIGNED,
        )
        db.add(issue)
        db.commit()

        self.issue_id = issue.id
        self.officer_one_id = officer_one.id
        self.officer_two_id = officer_two.id
        db.close()

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_concurrent_claims_produce_one_owner_and_one_conflict(self):
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def worker(user_id):
            db = self.Session()
            try:
                officer = db.query(models.User).filter(
                    models.User.id == user_id
                ).first()
                barrier.wait(timeout=10)
                claimed, already = claim_issue(db, officer, self.issue_id)
                with lock:
                    outcomes.append(("claimed", user_id, already, claimed.assigned_officer_id))
            except IssueAlreadyClaimedError:
                with lock:
                    outcomes.append(("conflict", user_id, None, None))
            except Exception as exc:  # pragma: no cover - surfaced as a failure
                with lock:
                    outcomes.append(("error", user_id, repr(exc), None))
            finally:
                db.close()

        threads = [
            threading.Thread(target=worker, args=(self.officer_one_id,)),
            threading.Thread(target=worker, args=(self.officer_two_id,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive(), "claim thread did not finish")

        self.assertEqual(len(outcomes), 2, outcomes)
        successes = [row for row in outcomes if row[0] == "claimed"]
        conflicts = [row for row in outcomes if row[0] == "conflict"]
        errors = [row for row in outcomes if row[0] == "error"]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual(len(conflicts), 1, outcomes)
        self.assertEqual(errors, [], outcomes)

        winner_id = successes[0][1]
        db = self.Session()
        try:
            stored = db.query(models.Issue).filter(
                models.Issue.id == self.issue_id
            ).first()
            self.assertEqual(stored.assigned_officer_id, winner_id)
        finally:
            db.close()


# ---------------------------------------------------------------------------
# D. Status transitions and audit
# ---------------------------------------------------------------------------
class TestOfficerStatusTransitions(OfficerPortalTestBase):
    def _latest_audit(self, issue_id):
        self.db.expire_all()
        return (
            self.db.query(models.IssueStatusUpdate)
            .filter(models.IssueStatusUpdate.issue_id == issue_id)
            .order_by(models.IssueStatusUpdate.created_at.desc())
            .first()
        )

    def test_officer_must_claim_before_updating_status(self):
        response = self.client.patch(
            f"/api/v1/issues/{self.eligible_unclaimed.id}/status",
            json={"new_status": "acknowledged", "comment": "Starting inspection."},
            headers=bearer(self.token_roads),
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("claim", response.json()["detail"].lower())

    def test_assigned_officer_can_update_and_audit_is_recorded(self):
        response = self.client.patch(
            f"/api/v1/issues/{self.claimed_by_me.id}/status",
            json={
                "new_status": "resolved",
                "comment": "Repair completed and verified on site.",
            },
            headers=bearer(self.token_roads),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "resolved")
        self.assertIsNotNone(body["resolved_at"])

        self.db.expire_all()
        stored = self.db.query(models.Issue).filter(
            models.Issue.id == self.claimed_by_me.id
        ).first()
        self.assertEqual(stored.status, models.IssueStatus.RESOLVED)
        self.assertIsNotNone(stored.resolved_at)

        audit = self._latest_audit(self.claimed_by_me.id)
        self.assertIsNotNone(audit)
        self.assertEqual(audit.updated_by_id, self.officer_roads.id)
        self.assertEqual(audit.old_status, models.IssueStatus.IN_PROGRESS)
        self.assertEqual(audit.new_status, models.IssueStatus.RESOLVED)
        self.assertEqual(audit.comment, "Repair completed and verified on site.")

    def test_non_owner_officer_cannot_update(self):
        response = self.client.patch(
            f"/api/v1/issues/{self.claimed_by_me.id}/status",
            json={"new_status": "resolved", "comment": "Attempted update."},
            headers=bearer(self.token_roads_2),
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("another officer", response.json()["detail"].lower())

    def test_cross_department_update_rejected(self):
        response = self.client.patch(
            f"/api/v1/issues/{self.other_department.id}/status",
            json={"new_status": "acknowledged", "comment": "Attempted update."},
            headers=bearer(self.token_roads),
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("not authorized", response.json()["detail"].lower())

    def test_missing_and_blank_comments_rejected(self):
        for payload in (
            {"new_status": "resolved"},
            {"new_status": "resolved", "comment": ""},
            {"new_status": "resolved", "comment": "   "},
        ):
            with self.subTest(payload=payload):
                response = self.client.patch(
                    f"/api/v1/issues/{self.claimed_by_me.id}/status",
                    json=payload,
                    headers=bearer(self.token_roads),
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("comment", response.json()["detail"].lower())

        self.db.expire_all()
        stored = self.db.query(models.Issue).filter(
            models.Issue.id == self.claimed_by_me.id
        ).first()
        self.assertEqual(stored.status, models.IssueStatus.IN_PROGRESS)

    def test_invalid_transition_rejected(self):
        response = self.client.patch(
            f"/api/v1/issues/{self.claimed_by_me.id}/status",
            json={"new_status": "acknowledged", "comment": "Invalid backwards move."},
            headers=bearer(self.token_roads),
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("invalid status transition", response.json()["detail"].lower())

        admin_response = self.client.patch(
            f"/api/v1/issues/{self.resolved_mine.id}/status",
            json={"new_status": "reported", "comment": "Administrative reopen attempt."},
            headers=bearer(self.token_admin),
        )
        self.assertEqual(admin_response.status_code, 400, admin_response.text)
        self.assertIn("invalid status transition", admin_response.json()["detail"].lower())

    def test_resolved_at_is_not_set_on_non_resolved_transition(self):
        response = self.client.patch(
            f"/api/v1/issues/{self.assigned_reported.id}/status",
            json={"new_status": "acknowledged", "comment": "Inspection scheduled."},
            headers=bearer(self.token_roads),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["resolved_at"])

        self.db.expire_all()
        stored = self.db.query(models.Issue).filter(
            models.Issue.id == self.assigned_reported.id
        ).first()
        self.assertIsNone(stored.resolved_at)

    def test_historical_records_remain_immutable(self):
        response = self.client.patch(
            f"/api/v1/issues/{self.historical.id}/status",
            json={"new_status": "reported", "comment": "Attempted historical edit."},
            headers=bearer(self.token_roads),
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("immutable", response.json()["detail"].lower())

        self.db.expire_all()
        stored = self.db.query(models.Issue).filter(
            models.Issue.id == self.historical.id
        ).first()
        self.assertEqual(stored.status, models.IssueStatus.RESOLVED)

    def test_unsupported_status_value_rejected(self):
        response = self.client.patch(
            f"/api/v1/issues/{self.claimed_by_me.id}/status",
            json={"new_status": "closed", "comment": "Unsupported status."},
            headers=bearer(self.token_roads),
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_officer_cannot_update_manual_review_or_cross_city_records(self):
        review = self.client.patch(
            f"/api/v1/issues/{self.review_assigned.id}/status",
            json={"new_status": "acknowledged", "comment": "Attempted review update."},
            headers=bearer(self.token_roads),
        )
        self.assertEqual(review.status_code, 403, review.text)
        self.assertIn("verified automatic routing", review.json()["detail"].lower())

        cross_city = self.client.patch(
            f"/api/v1/issues/{self.cross_city_assigned.id}/status",
            json={"new_status": "acknowledged", "comment": "Attempted cross-city update."},
            headers=bearer(self.token_roads),
        )
        self.assertEqual(cross_city.status_code, 403, cross_city.text)
        self.assertIn("another city", cross_city.json()["detail"].lower())

    def test_admin_authorization_is_preserved(self):
        response = self.client.patch(
            f"/api/v1/issues/{self.other_department.id}/status",
            json={"new_status": "acknowledged", "comment": "Administrative review."},
            headers=bearer(self.token_admin),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "acknowledged")


# ---------------------------------------------------------------------------
# E. Dashboard analytics
# ---------------------------------------------------------------------------
class TestOfficerDashboard(OfficerPortalTestBase):
    def test_overview_counts_match_real_scoped_records(self):
        response = self.client.get(
            "/api/v1/officer/overview", headers=bearer(self.token_roads)
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        counts = body["counts"]

        self.assertEqual(body["department"]["id"], self.dept_roads.id)
        self.assertEqual(body["department"]["city"], CITY_GURUGRAM)
        self.assertEqual(counts["total_live_routed"], 7)
        self.assertEqual(counts["new_reported"], 3)
        self.assertEqual(counts["unclaimed"], 2)
        self.assertEqual(counts["assigned_to_me"], 3)
        self.assertEqual(counts["my_open"], 2)
        self.assertEqual(counts["in_progress"], 1)
        self.assertEqual(counts["resolved"], 2)
        self.assertIsNone(counts["awaiting_review"])
        self.assertFalse(counts["awaiting_review_visible"])
        self.assertFalse(body["resolution_timing_available"])

    def test_overview_excludes_historical_other_department_and_other_city(self):
        response = self.client.get(
            "/api/v1/officer/overview", headers=bearer(self.token_garbage)
        )
        self.assertEqual(response.status_code, 200, response.text)
        counts = response.json()["counts"]
        self.assertEqual(counts["total_live_routed"], 1)
        self.assertEqual(counts["unclaimed"], 1)
        self.assertEqual(counts["assigned_to_me"], 0)
        self.assertEqual(counts["resolved"], 0)

    def test_overview_requires_an_authenticated_officer(self):
        anonymous = self.client.get("/api/v1/officer/overview")
        self.assertEqual(anonymous.status_code, 401)

        citizen = self.client.get(
            "/api/v1/officer/overview", headers=bearer(self.token_a)
        )
        self.assertEqual(citizen.status_code, 403)

        admin = self.client.get(
            "/api/v1/officer/overview", headers=bearer(self.token_admin)
        )
        self.assertEqual(admin.status_code, 403)


# ---------------------------------------------------------------------------
# F. Detail and evidence
# ---------------------------------------------------------------------------
class TestOfficerDetailAndEvidence(OfficerPortalTestBase):
    def test_detail_returns_real_complaint_data_images_history_and_permissions(self):
        response = self.client.get(
            f"/api/v1/officer/issues/{self.eligible_unclaimed.id}",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()

        self.assertEqual(body["issue"]["title"], "TEST OFFICER ELIGIBLE POTHOLE")
        self.assertIn("TEST OFFICER ELIGIBLE DESCRIPTION", body["issue"]["description"])
        self.assertEqual(body["department"]["id"], self.dept_roads.id)
        self.assertEqual(len(body["images"]), 1)
        self.assertEqual(body["images"][0]["id"], self.image_eligible.id)
        self.assertTrue(body["images"][0]["image_url"].startswith("evidence/"))
        self.assertFalse(Path(body["images"][0]["image_url"]).is_absolute())
        self.assertTrue(body["permissions"]["can_claim"])
        self.assertFalse(body["permissions"]["can_update"])
        self.assertFalse(body["permissions"]["is_assigned_to_me"])
        self.assertFalse(body["permissions"]["assigned_to_another_officer"])

        for key in (
            "category_source", "category_score_kind", "category_needs_review",
            "category_review_reason", "priority_reasons", "routing_status",
            "routing_reason", "ticket_number", "created_at",
        ):
            self.assertIn(key, body["issue"])

    def test_assigned_issue_detail_allows_update_and_shows_history(self):
        response = self.client.get(
            f"/api/v1/officer/issues/{self.claimed_by_me.id}",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["permissions"]["can_update"])
        self.assertTrue(body["permissions"]["is_assigned_to_me"])
        self.assertFalse(body["permissions"]["can_claim"])
        self.assertGreaterEqual(len(body["history"]), 1)
        self.assertEqual(body["history"][0]["comment"], "Crew dispatched to inspect the road.")

    def test_other_officer_sees_read_only_assignment(self):
        response = self.client.get(
            f"/api/v1/officer/issues/{self.claimed_by_me.id}",
            headers=bearer(self.token_roads_2),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["permissions"]["can_update"])
        self.assertFalse(body["permissions"]["can_claim"])
        self.assertTrue(body["permissions"]["assigned_to_another_officer"])

    def test_same_department_officer_can_fetch_evidence_content(self):
        response = self.client.get(
            f"/api/v1/issues/{self.eligible_unclaimed.id}/images/"
            f"{self.image_eligible.id}/content",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, b"isolated-test-evidence")
        self.assertEqual(response.headers.get("cache-control"), "private, no-store")

    def test_cross_department_detail_and_evidence_denied(self):
        detail = self.client.get(
            f"/api/v1/officer/issues/{self.other_department.id}",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(detail.status_code, 404)

        content = self.client.get(
            f"/api/v1/issues/{self.other_department.id}/images/"
            f"{self.image_other.id}/content",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(content.status_code, 404)

    def test_historical_detail_and_claim_rejected_but_existing_evidence_scope_preserved(self):
        detail = self.client.get(
            f"/api/v1/officer/issues/{self.historical.id}",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(detail.status_code, 404)

        claim = self.client.post(
            f"/api/v1/officer/issues/{self.historical.id}/claim",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(claim.status_code, 404)

        # The pre-existing department-scoped read endpoint is deliberately not
        # weakened by Task 9: a same-department officer can still read a
        # historical record's evidence, while another department cannot.
        same_department = self.client.get(
            f"/api/v1/issues/{self.historical.id}/images/"
            f"{self.image_historical.id}/content",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(same_department.status_code, 200, same_department.text)

        other_department = self.client.get(
            f"/api/v1/issues/{self.historical.id}/images/"
            f"{self.image_historical.id}/content",
            headers=bearer(self.token_garbage),
        )
        self.assertEqual(other_department.status_code, 404)

    def test_detail_exposes_no_absolute_paths_or_private_contact_fields(self):
        response = self.client.get(
            f"/api/v1/officer/issues/{self.eligible_unclaimed.id}",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(response.status_code, 200, response.text)
        text = response.text
        self.assertNotIn(self.upload_dir, text)
        for forbidden in ("password", "password_hash", "phone", "token"):
            self.assertNotIn(forbidden, text.lower())


# ---------------------------------------------------------------------------
# G. Frontend route, contracts and safety
# ---------------------------------------------------------------------------
class TestOfficerPortalFrontend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = PORTAL_PAGE.read_text(encoding="utf-8")
        cls.client = TestClient(app)

    def test_officer_route_serves_portal(self):
        response = self.client.get("/officer")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PORTAL_PAGE.read_bytes())
        self.assertIn("text/html", response.headers.get("content-type", ""))

    def test_existing_dashboard_and_citizen_routes_are_unchanged(self):
        historical = self.client.get("/")
        self.assertEqual(historical.status_code, 200)
        self.assertEqual(historical.content, HISTORICAL_PAGE.read_bytes())

        citizen = self.client.get("/citizen")
        self.assertEqual(citizen.status_code, 200)
        self.assertEqual(citizen.content, CITIZEN_PAGE.read_bytes())

    def test_frontend_uses_the_expected_api_contracts(self):
        for endpoint in (
            "/auth/login",
            "/auth/me",
            "/officer/overview",
            "/officer/issues",
            "/claim",
            "/issues/",
            "/status",
            "/images/",
        ):
            self.assertIn(endpoint, self.page, f"missing endpoint contract: {endpoint}")

        for text in (
            "My assigned complaints",
            "Unclaimed",
            "Live queue",
            "AI classification",
            "Citizen evidence",
            "Status history",
            "Routing",
            "category_score_kind",
            "category_needs_review",
            "routing_reason",
            "status-comment",
            "Mandatory remark",
        ):
            self.assertIn(text, self.page, f"missing interface text: {text}")

    def test_frontend_requires_authenticated_officer_role(self):
        self.assertIn('String(me.role).toLowerCase() !== "officer"', self.page)
        self.assertIn("restricted to authorised municipal officers", self.page)
        self.assertNotIn("/auth/register", self.page)

    def test_frontend_uses_safe_dom_apis_only(self):
        self.assertNotIn("innerHTML", self.page)
        self.assertNotIn("outerHTML", self.page)
        self.assertNotIn("insertAdjacentHTML", self.page)
        self.assertNotIn("document.write", self.page)
        self.assertNotIn("localStorage", self.page)
        self.assertNotIn("eval(", self.page)
        self.assertNotIn("new Function(", self.page)
        self.assertIn("textContent", self.page)
        self.assertIn("replaceChildren", self.page)
        self.assertIn("createTextNode", self.page)

    def test_frontend_has_no_fabricated_records_or_metrics(self):
        self.assertIsNone(re.search(r"UIMRS-\d{4}-\d{3,}", self.page))
        self.assertNotIn("sample complaint", self.page.lower())
        self.assertNotIn("demo complaint", self.page.lower())
        self.assertNotIn("fake metric", self.page.lower())
        self.assertIn("Loading officer", self.page)
        self.assertNotIn("Officer Name", self.page)

    def test_logout_clears_officer_session_data(self):
        self.assertIn("sessionStorage", self.page)
        self.assertIn("removeItem(TOKEN_KEY)", self.page)
        self.assertIn("resetSessionState", self.page)
        self.assertIn("clearEvidenceUrls", self.page)

    def test_status_form_requires_a_non_empty_comment(self):
        self.assertIn("Mandatory remark", self.page)
        self.assertIn("textarea.required = true", self.page)
        self.assertIn("A non-empty remark is required", self.page)

    def test_evidence_thumbnails_do_not_deadlock_on_lazy_loading(self):
        # Evidence is fetched as an authenticated blob and the <img> stays
        # detached until its load event fires. A lazy image would never start
        # loading, so the handlers must be attached before src and loading must
        # be eager.
        self.assertIn('img.loading = "eager"', self.page)
        self.assertLess(
            self.page.index('img.addEventListener("load"'),
            self.page.index("img.src = url"),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
