"""UIMRS Task 10 — secure administrator oversight portal.

Covers:
A. Administrator-only API authorization and identity handling.
B. Real operational overview counts with live/historical separation.
C. Manual jurisdiction review queue scope and validated routing decisions.
D. Officer provisioning, department reassignment and historical preservation.
E. Immutable administrative audit log, filters, pagination and atomicity.
F. Administrator portal route, safe DOM, session handling and unchanged portals.
G. Schema guard, migration head and isolated-database safety.

All API tests use an isolated in-memory SQLite database and a temporary upload
directory. The migration test uses a temporary file database. The real project
databases are never opened or written by this module.
"""
import json
import re
import shutil
import sqlite3
import sys
import tempfile
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
from app.services.admin_audit import (
    ACTION_ISSUE_LEFT_UNASSIGNED,
    ACTION_ISSUE_ROUTED,
    ACTION_OFFICER_CREATED,
    ACTION_OFFICER_DEPARTMENT_CHANGED,
    record_admin_audit,
)
from app.services.admin_operations import route_issue
from app.services.classification_service import CITY_BENGALURU, CITY_GURUGRAM
from app.services.department_routing import (
    ROUTING_MODE_AUTOMATIC,
    ROUTING_MODE_DISABLED,
    ROUTING_MODE_MANUAL_REVIEW,
    ROUTING_STATUS_ASSIGNED,
    ROUTING_STATUS_UNASSIGNED_NO_MAPPING,
    ROUTING_STATUS_UNASSIGNED_REVIEW,
)
from app.db_guard import AUDIT_LOG_TABLE, REQUIRED_SCHEMA
from app.db_migrate import HEAD_REVISION, prepare_database, current_revision

TEST_PASSWORD = "AdminPortal#2026"
ADMIN_PAGE = PROJECT_ROOT / "frontend" / "admin_portal.html"
OFFICER_PAGE = PROJECT_ROOT / "frontend" / "officer_portal.html"
CITIZEN_PAGE = PROJECT_ROOT / "frontend" / "citizen_portal.html"
VERIFIED_SOURCE = "https://mcg.gov.in/contact/office-contacts"
VERIFIED_DATE = "2026-11-14"
UNSUPPORTED_CITY = "Mumbai, Maharashtra, India"

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


def make_department(name, category, city, mode=ROUTING_MODE_AUTOMATIC, active=True,
                    verified=True):
    return models.Department(
        name=name,
        handles_category=category,
        city=city,
        source_agency=name,
        routing_mode=mode,
        verification_source_url=VERIFIED_SOURCE if verified else None,
        verification_date=VERIFIED_DATE if verified else None,
        is_active=active,
    )


class AdminPortalTestBase(unittest.TestCase):
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
        self.upload_dir = tempfile.mkdtemp(prefix="uimrs_admin_evidence_")
        self.settings.UPLOAD_DIR = self.upload_dir

        self.db.query(models.AdminAuditLog).delete()
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
            "description": "Isolated Task 10 fixture description, not a real complaint.",
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
            "category_needs_review": False,
        }
        values.update(overrides)
        issue = models.Issue(ticket_number=ticket, **values)
        self.db.add(issue)
        self.db.commit()
        self.db.refresh(issue)
        return issue

    def _setup_fixtures(self):
        self.dept_roads = make_department(
            "TEST_ADMIN_GURUGRAM_ROADS",
            models.IssueCategory.ROAD_DAMAGE,
            CITY_GURUGRAM,
        )
        self.dept_garbage = make_department(
            "TEST_ADMIN_GURUGRAM_GARBAGE",
            models.IssueCategory.GARBAGE_WASTE,
            CITY_GURUGRAM,
        )
        self.dept_streetlight = make_department(
            "TEST_ADMIN_GURUGRAM_STREETLIGHT",
            models.IssueCategory.STREETLIGHT,
            CITY_GURUGRAM,
            mode=ROUTING_MODE_MANUAL_REVIEW,
        )
        self.dept_disabled = make_department(
            "TEST_ADMIN_GURUGRAM_WATER_DISABLED",
            models.IssueCategory.WATER_SUPPLY,
            CITY_GURUGRAM,
            mode=ROUTING_MODE_DISABLED,
        )
        self.dept_inactive = make_department(
            "TEST_ADMIN_GURUGRAM_DRAINAGE_INACTIVE",
            models.IssueCategory.DRAINAGE_SEWAGE,
            CITY_GURUGRAM,
            active=False,
        )
        self.dept_unverified = make_department(
            "TEST_ADMIN_GURUGRAM_TRAFFIC_UNVERIFIED",
            models.IssueCategory.TRAFFIC_SIGNAL,
            CITY_GURUGRAM,
            verified=False,
        )
        self.dept_bengaluru = make_department(
            "TEST_ADMIN_BENGALURU_ROADS",
            models.IssueCategory.ROAD_DAMAGE,
            CITY_BENGALURU,
            mode=ROUTING_MODE_MANUAL_REVIEW,
            verified=False,
        )
        self.dept_unsupported = make_department(
            "TEST_ADMIN_UNSUPPORTED_CITY",
            models.IssueCategory.ROAD_DAMAGE,
            UNSUPPORTED_CITY,
        )
        self.db.add_all([
            self.dept_roads, self.dept_garbage, self.dept_streetlight,
            self.dept_disabled, self.dept_inactive, self.dept_unverified,
            self.dept_bengaluru, self.dept_unsupported,
        ])
        self.db.commit()

        self.admin = self._user(
            "TEST ADMIN USER", "task10.admin@example.com", models.UserRole.ADMIN
        )
        self.admin_two = self._user(
            "TEST ADMIN USER TWO", "task10.admin.two@example.com",
            models.UserRole.ADMIN,
        )
        self.citizen = self._user(
            "TEST ADMIN CITIZEN", "task10.citizen@example.com",
            models.UserRole.CITIZEN,
        )
        self.officer = self._user(
            "TEST ADMIN OFFICER", "task10.officer@example.com",
            models.UserRole.OFFICER, self.dept_roads.id,
        )
        self.officer_two = self._user(
            "TEST ADMIN OFFICER TWO", "task10.officer.two@example.com",
            models.UserRole.OFFICER, self.dept_garbage.id,
        )
        self.db.add_all([self.admin, self.admin_two, self.citizen, self.officer, self.officer_two])
        self.db.commit()

        self.token_admin = create_access_token(self.admin)
        self.token_admin_two = create_access_token(self.admin_two)
        self.token_citizen = create_access_token(self.citizen)
        self.token_officer = create_access_token(self.officer)

        self.review_road = self._issue(
            "TASK10-REVIEW-ROAD", self.citizen, None,
            title="TEST ADMIN REVIEW ROAD",
            routing_status=ROUTING_STATUS_UNASSIGNED_REVIEW,
            category_needs_review=True,
            priority_score=8.0,
            priority_level=models.PriorityLevel.HIGH,
        )
        self.review_nomap = self._issue(
            "TASK10-REVIEW-NOMAP", self.citizen, None,
            title="TEST ADMIN REVIEW NO MAPPING",
            category=models.IssueCategory.GARBAGE_WASTE,
            routing_status=ROUTING_STATUS_UNASSIGNED_NO_MAPPING,
        )
        self.review_no_candidate = self._issue(
            "TASK10-REVIEW-OTHER", self.citizen, None,
            title="TEST ADMIN REVIEW OTHER",
            category=models.IssueCategory.OTHER,
            routing_status=ROUTING_STATUS_UNASSIGNED_REVIEW,
        )
        self.assigned_road = self._issue(
            "TASK10-ASSIGNED-ROAD", self.citizen, self.dept_roads,
            title="TEST ADMIN ASSIGNED ROAD",
            status=models.IssueStatus.ACKNOWLEDGED,
            routing_status=ROUTING_STATUS_ASSIGNED,
        )
        self.closed_review = self._issue(
            "TASK10-CLOSED-REVIEW", self.citizen, None,
            title="TEST ADMIN CLOSED REVIEW",
            status=models.IssueStatus.RESOLVED,
            routing_status=ROUTING_STATUS_UNASSIGNED_REVIEW,
            resolved_at=datetime.now(timezone.utc),
        )
        self.review_with_officer = self._issue(
            "TASK10-REVIEW-OFFICER", self.citizen, None,
            title="TEST ADMIN REVIEW WITH OFFICER",
            routing_status=ROUTING_STATUS_UNASSIGNED_REVIEW,
            assigned_officer_id=self.officer.id,
        )
        self.review_bengaluru = self._issue(
            "TASK10-REVIEW-BLR", self.citizen, None,
            title="TEST ADMIN REVIEW BENGALURU",
            city=CITY_BENGALURU,
            latitude=12.9716,
            longitude=77.5946,
            routing_status=ROUTING_STATUS_UNASSIGNED_REVIEW,
        )
        self.review_streetlight = self._issue(
            "TASK10-REVIEW-STREETLIGHT", self.citizen, None,
            title="TEST ADMIN REVIEW STREETLIGHT",
            category=models.IssueCategory.STREETLIGHT,
            routing_status=ROUTING_STATUS_UNASSIGNED_REVIEW,
        )
        self.review_water = self._issue(
            "TASK10-REVIEW-WATER", self.citizen, None,
            title="TEST ADMIN REVIEW WATER",
            category=models.IssueCategory.WATER_SUPPLY,
            routing_status=ROUTING_STATUS_UNASSIGNED_REVIEW,
        )
        self.review_drainage = self._issue(
            "TASK10-REVIEW-DRAINAGE", self.citizen, None,
            title="TEST ADMIN REVIEW DRAINAGE",
            category=models.IssueCategory.DRAINAGE_SEWAGE,
            routing_status=ROUTING_STATUS_UNASSIGNED_REVIEW,
        )
        self.review_traffic = self._issue(
            "TASK10-REVIEW-TRAFFIC", self.citizen, None,
            title="TEST ADMIN REVIEW TRAFFIC",
            category=models.IssueCategory.TRAFFIC_SIGNAL,
            routing_status=ROUTING_STATUS_UNASSIGNED_REVIEW,
        )
        self.assigned_garbage = self._issue(
            "TASK10-ASSIGNED-GARBAGE", self.citizen, self.dept_garbage,
            title="TEST ADMIN ASSIGNED GARBAGE",
            category=models.IssueCategory.GARBAGE_WASTE,
            status=models.IssueStatus.IN_PROGRESS,
            routing_status=ROUTING_STATUS_ASSIGNED,
            assigned_officer_id=self.officer_two.id,
        )
        self.historical_road = self._issue(
            "TASK10-HIST-ROAD", self.admin, self.dept_bengaluru,
            title="TEST ADMIN HISTORICAL ROAD",
            category=models.IssueCategory.ROAD_DAMAGE,
            status=models.IssueStatus.RESOLVED,
            city=CITY_BENGALURU,
            latitude=12.9716,
            longitude=77.5946,
            is_historical=True,
            routing_status=None,
            source_record_id="TASK10-HIST-SOURCE-1",
            source_agency="BBMP",
        )
        self.historical_garbage = self._issue(
            "TASK10-HIST-GARBAGE", self.admin, None,
            title="TEST ADMIN HISTORICAL GARBAGE",
            category=models.IssueCategory.GARBAGE_WASTE,
            status=models.IssueStatus.RESOLVED,
            city=CITY_BENGALURU,
            latitude=12.9716,
            longitude=77.5946,
            is_historical=True,
            routing_status=None,
            source_record_id="TASK10-HIST-SOURCE-2",
            source_agency="BBMP",
        )
        self.historical_gurugram_roads = self._issue(
            "TASK10-HIST-GUR-ROAD", self.admin, self.dept_roads,
            title="TEST ADMIN HISTORICAL GURUGRAM ROAD",
            status=models.IssueStatus.RESOLVED,
            is_historical=True,
            routing_status=None,
            source_record_id="TASK10-HIST-SOURCE-3",
            source_agency="MCG",
        )
        self.db.commit()

    def _get(self, path, token=None):
        headers = bearer(token) if token else {}
        return self.client.get(path, headers=headers)

    def _post(self, path, token=None, payload=None):
        headers = bearer(token) if token else {}
        return self.client.post(path, headers=headers, json=payload or {})

    def _patch(self, path, token=None, payload=None):
        headers = bearer(token) if token else {}
        return self.client.patch(path, headers=headers, json=payload or {})

    def _audit_rows(self):
        self.db.expire_all()
        return (
            self.db.query(models.AdminAuditLog)
            .order_by(models.AdminAuditLog.created_at.asc())
            .all()
        )


# ---------------------------------------------------------------------------
# A. Authorization and identity
# ---------------------------------------------------------------------------
class TestAdminAuthorization(AdminPortalTestBase):
    ADMIN_ENDPOINTS = (
        "/api/v1/admin/overview",
        "/api/v1/admin/issues",
        "/api/v1/admin/review-queue",
        "/api/v1/admin/officers",
        "/api/v1/admin/departments",
        "/api/v1/admin/audit-logs",
    )

    def test_admin_can_read_every_admin_endpoint(self):
        for path in self.ADMIN_ENDPOINTS:
            with self.subTest(path=path):
                response = self._get(path, self.token_admin)
                self.assertEqual(response.status_code, 200, response.text)

    def test_citizen_is_forbidden_on_every_admin_endpoint(self):
        for path in self.ADMIN_ENDPOINTS:
            with self.subTest(path=path):
                response = self._get(path, self.token_citizen)
                self.assertEqual(response.status_code, 403, response.text)

    def test_officer_is_forbidden_on_every_admin_endpoint(self):
        for path in self.ADMIN_ENDPOINTS:
            with self.subTest(path=path):
                response = self._get(path, self.token_officer)
                self.assertEqual(response.status_code, 403, response.text)

    def test_missing_token_is_unauthorized(self):
        response = self.client.get("/api/v1/admin/overview")
        self.assertEqual(response.status_code, 401)

    def test_invalid_token_is_unauthorized(self):
        response = self.client.get(
            "/api/v1/admin/overview",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        self.assertEqual(response.status_code, 401)

    def test_expired_token_is_unauthorized(self):
        expired = create_access_token(self.admin, expires_delta=timedelta(seconds=-5))
        response = self._get("/api/v1/admin/overview", expired)
        self.assertEqual(response.status_code, 401)

    def test_token_role_claim_cannot_escalate_a_citizen(self):
        forged = create_access_token(self.citizen, role="admin")
        response = self._get("/api/v1/admin/overview", forged)
        self.assertEqual(response.status_code, 403, response.text)

    def test_public_registration_cannot_create_an_admin(self):
        response = self.client.post(
            "/api/v1/auth/register",
            json={
                "name": "Registration Escalation Attempt",
                "email": "task10.escalation@example.com",
                "password": "Escalation#2026",
                "role": "admin",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["role"], "citizen")


# ---------------------------------------------------------------------------
# B. Overview
# ---------------------------------------------------------------------------
class TestAdminOverview(AdminPortalTestBase):
    def test_overview_reports_real_separated_counts(self):
        response = self._get("/api/v1/admin/overview", self.token_admin)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()

        live = body["live"]
        self.assertEqual(live["total"], 12)
        self.assertEqual(live["statuses"]["reported"], 9)
        self.assertEqual(live["statuses"]["acknowledged"], 1)
        self.assertEqual(live["statuses"]["in_progress"], 1)
        self.assertEqual(live["statuses"]["resolved"], 1)
        self.assertEqual(live["statuses"]["rejected"], 0)
        self.assertEqual(live["statuses"]["duplicate"], 0)
        self.assertEqual(live["assigned"], 2)
        self.assertEqual(live["unassigned"], 10)
        self.assertEqual(live["awaiting_review"], 9)
        self.assertEqual(live["needs_ai_review"], 1)

        workload = {row["department_id"]: row["count"] for row in live["department_workload"]}
        self.assertEqual(workload.get(self.dept_roads.id), 1)
        self.assertEqual(workload.get(self.dept_garbage.id), 1)

        cities = {row["city"]: row["count"] for row in live["city_distribution"]}
        self.assertEqual(cities.get(CITY_GURUGRAM), 11)
        self.assertEqual(cities.get(CITY_BENGALURU), 1)

        historical = body["historical"]
        self.assertEqual(historical["total"], 3)
        agencies = {row["source_agency"]: row["count"] for row in historical["source_agency_distribution"]}
        self.assertEqual(agencies.get("BBMP"), 2)
        self.assertEqual(agencies.get("MCG"), 1)

        self.assertFalse(body["resolution_timing_available"])

    def test_historical_records_never_enter_live_counts(self):
        response = self._get("/api/v1/admin/overview", self.token_admin)
        body = response.json()
        # 12 live + 3 historical: the live total must exclude every historical row.
        self.assertEqual(body["live"]["total"], 12)
        self.assertEqual(body["historical"]["total"], 3)
        self.assertNotEqual(body["live"]["total"], 15)

    def test_overview_city_filter_is_applied_to_live_and_historical(self):
        response = self._get(
            f"/api/v1/admin/overview?city={CITY_BENGALURU.replace(' ', '%20')}",
            self.token_admin,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["filters"]["city"], CITY_BENGALURU)
        self.assertEqual(body["live"]["total"], 1)
        self.assertEqual(body["historical"]["total"], 2)

    def test_overview_department_filter_is_applied(self):
        response = self._get(
            f"/api/v1/admin/overview?department_id={self.dept_roads.id}",
            self.token_admin,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["filters"]["department_id"], self.dept_roads.id)
        self.assertEqual(body["live"]["total"], 1)
        self.assertEqual(body["live"]["department_workload"][0]["department_id"], self.dept_roads.id)


# ---------------------------------------------------------------------------
# C. Manual jurisdiction review
# ---------------------------------------------------------------------------
class TestAdminReviewQueue(AdminPortalTestBase):
    def test_review_queue_contains_only_live_reviewable_records(self):
        response = self._get("/api/v1/admin/review-queue?limit=200", self.token_admin)
        self.assertEqual(response.status_code, 200, response.text)
        ids = {row["id"] for row in response.json()}

        self.assertIn(self.review_road.id, ids)
        self.assertIn(self.review_nomap.id, ids)
        self.assertIn(self.review_no_candidate.id, ids)
        self.assertIn(self.review_bengaluru.id, ids)
        self.assertIn(self.review_streetlight.id, ids)
        self.assertIn(self.review_water.id, ids)
        self.assertIn(self.review_drainage.id, ids)
        self.assertIn(self.review_traffic.id, ids)

        # Historical, already-assigned, officer-owned and closed records are excluded.
        self.assertNotIn(self.historical_road.id, ids)
        self.assertNotIn(self.historical_garbage.id, ids)
        self.assertNotIn(self.assigned_road.id, ids)
        self.assertNotIn(self.closed_review.id, ids)
        self.assertNotIn(self.review_with_officer.id, ids)

    def test_review_queue_filters_by_city_and_category(self):
        response = self._get(
            f"/api/v1/admin/review-queue?city={CITY_BENGALURU.replace(' ', '%20')}",
            self.token_admin,
        )
        self.assertEqual(response.status_code, 200, response.text)
        rows = response.json()
        self.assertEqual([row["id"] for row in rows], [self.review_bengaluru.id])

        response = self._get(
            "/api/v1/admin/review-queue?category=Water_Supply", self.token_admin
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row["id"] for row in response.json()], [self.review_water.id])

    def test_issue_detail_exposes_eligible_candidates_only(self):
        response = self._get(
            f"/api/v1/admin/issues/{self.review_road.id}", self.token_admin
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["can_route"])
        candidates = {row["id"]: row for row in body["candidates"]}
        self.assertIn(self.dept_roads.id, candidates)
        self.assertTrue(candidates[self.dept_roads.id]["selectable"])
        # Same-city/same-category only: the Bengaluru road department is absent.
        self.assertNotIn(self.dept_bengaluru.id, candidates)

    def test_disabled_mapping_is_listed_but_not_selectable(self):
        response = self._get(
            f"/api/v1/admin/issues/{self.review_water.id}", self.token_admin
        )
        body = response.json()
        candidates = {row["id"]: row for row in body["candidates"]}
        self.assertIn(self.dept_disabled.id, candidates)
        self.assertFalse(candidates[self.dept_disabled.id]["selectable"])
        self.assertTrue(candidates[self.dept_disabled.id]["blocked_reasons"])

    def test_route_to_valid_department_assigns_department_and_audits(self):
        response = self._post(
            f"/api/v1/admin/issues/{self.review_road.id}/route",
            self.token_admin,
            {"department_id": self.dept_roads.id, "reason": "Verified MCG roads ownership confirmed by source."},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["action"], ACTION_ISSUE_ROUTED)
        self.assertEqual(body["issue"]["department_id"], self.dept_roads.id)
        self.assertEqual(body["issue"]["routing_status"], ROUTING_STATUS_ASSIGNED)
        self.assertIsNone(body["issue"]["assigned_officer_id"])
        self.assertIn("Verified MCG roads ownership", body["issue"]["routing_reason"])

        self.db.expire_all()
        issue = self.db.query(models.Issue).filter(models.Issue.id == self.review_road.id).first()
        self.assertEqual(issue.department_id, self.dept_roads.id)
        self.assertEqual(issue.routing_status, ROUTING_STATUS_ASSIGNED)
        self.assertIsNone(issue.assigned_officer_id)

        audits = [row for row in self._audit_rows() if row.action == ACTION_ISSUE_ROUTED]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].entity_id, self.review_road.id)
        self.assertEqual(audits[0].actor_user_id, self.admin.id)
        self.assertEqual(audits[0].before_state["department_id"], None)
        self.assertEqual(audits[0].after_state["department_id"], self.dept_roads.id)

    def test_route_accepts_a_verified_manual_review_mapping(self):
        response = self._post(
            f"/api/v1/admin/issues/{self.review_streetlight.id}/route",
            self.token_admin,
            {"department_id": self.dept_streetlight.id, "reason": "Manual review confirmed the streetlight wing owns this asset."},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["issue"]["department_id"], self.dept_streetlight.id)

    def test_route_rejects_city_mismatch(self):
        response = self._post(
            f"/api/v1/admin/issues/{self.review_road.id}/route",
            self.token_admin,
            {"department_id": self.dept_bengaluru.id, "reason": "Attempting a cross-city assignment."},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.db.expire_all()
        issue = self.db.query(models.Issue).filter(models.Issue.id == self.review_road.id).first()
        self.assertIsNone(issue.department_id)

    def test_route_rejects_category_mismatch(self):
        response = self._post(
            f"/api/v1/admin/issues/{self.review_road.id}/route",
            self.token_admin,
            {"department_id": self.dept_garbage.id, "reason": "Attempting a category mismatch assignment."},
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_route_rejects_disabled_mapping(self):
        response = self._post(
            f"/api/v1/admin/issues/{self.review_water.id}/route",
            self.token_admin,
            {"department_id": self.dept_disabled.id, "reason": "Attempting to route through a disabled mapping."},
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_route_rejects_inactive_department(self):
        response = self._post(
            f"/api/v1/admin/issues/{self.review_drainage.id}/route",
            self.token_admin,
            {"department_id": self.dept_inactive.id, "reason": "Attempting to route to an inactive department."},
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_route_rejects_unverified_mapping(self):
        response = self._post(
            f"/api/v1/admin/issues/{self.review_traffic.id}/route",
            self.token_admin,
            {"department_id": self.dept_unverified.id, "reason": "Attempting to route to an unverified mapping."},
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_route_rejects_unknown_department(self):
        response = self._post(
            f"/api/v1/admin/issues/{self.review_road.id}/route",
            self.token_admin,
            {"department_id": "00000000-0000-0000-0000-000000000000", "reason": "Attempting to route to a fabricated department."},
        )
        self.assertEqual(response.status_code, 404, response.text)

    def test_route_rejects_historical_record(self):
        response = self._post(
            f"/api/v1/admin/issues/{self.historical_road.id}/route",
            self.token_admin,
            {"department_id": self.dept_bengaluru.id, "reason": "Attempting to modify a historical record."},
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_route_rejects_already_assigned_record(self):
        response = self._post(
            f"/api/v1/admin/issues/{self.assigned_road.id}/route",
            self.token_admin,
            {"department_id": self.dept_roads.id, "reason": "Attempting to re-route an already assigned record."},
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_route_rejects_closed_record(self):
        response = self._post(
            f"/api/v1/admin/issues/{self.closed_review.id}/route",
            self.token_admin,
            {"department_id": self.dept_roads.id, "reason": "Attempting to route a closed complaint."},
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_route_requires_meaningful_justification(self):
        response = self._post(
            f"/api/v1/admin/issues/{self.review_road.id}/route",
            self.token_admin,
            {"department_id": self.dept_roads.id, "reason": "short"},
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_leave_unassigned_records_review_status_reason_and_audit(self):
        response = self._post(
            f"/api/v1/admin/issues/{self.review_nomap.id}/route",
            self.token_admin,
            {"department_id": None, "reason": "No verified department currently owns this category."},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["action"], ACTION_ISSUE_LEFT_UNASSIGNED)
        self.assertIsNone(body["issue"]["department_id"])
        self.assertEqual(body["issue"]["routing_status"], ROUTING_STATUS_UNASSIGNED_REVIEW)
        self.assertIn("No verified department currently owns", body["issue"]["routing_reason"])
        self.assertIsNone(body["issue"]["assigned_officer_id"])

        audits = [row for row in self._audit_rows() if row.action == ACTION_ISSUE_LEFT_UNASSIGNED]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].reason, "No verified department currently owns this category.")

    def test_route_does_not_auto_assign_an_officer(self):
        self._post(
            f"/api/v1/admin/issues/{self.review_road.id}/route",
            self.token_admin,
            {"department_id": self.dept_roads.id, "reason": "Verified ownership confirmed for the roads wing."},
        )
        self.db.expire_all()
        issue = self.db.query(models.Issue).filter(models.Issue.id == self.review_road.id).first()
        self.assertIsNone(issue.assigned_officer_id)

    def test_unknown_issue_returns_not_found(self):
        response = self._post(
            "/api/v1/admin/issues/does-not-exist/route",
            self.token_admin,
            {"department_id": self.dept_roads.id, "reason": "Attempting to route a missing complaint."},
        )
        self.assertEqual(response.status_code, 404, response.text)


# ---------------------------------------------------------------------------
# D. Officer management
# ---------------------------------------------------------------------------
class TestAdminOfficerManagement(AdminPortalTestBase):
    def _officer_payload(self, email="task10.new.officer@example.com",
                         department_id=None, **overrides):
        payload = {
            "name": "New Municipal Officer",
            "email": email,
            "phone": None,
            "password": "NewOfficer#2026",
            "department_id": department_id or self.dept_roads.id,
            "preferred_language": "en",
        }
        payload.update(overrides)
        return payload

    def test_admin_creates_officer_with_forced_role_and_hashed_password(self):
        response = self._post(
            "/api/v1/admin/officers", self.token_admin, self._officer_payload()
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["role"], "officer")
        self.assertEqual(body["department_id"], self.dept_roads.id)
        self.assertNotIn("password", body)
        self.assertNotIn("password_hash", body)

        self.db.expire_all()
        user = self.db.query(models.User).filter(
            models.User.email == "task10.new.officer@example.com"
        ).first()
        self.assertIsNotNone(user)
        self.assertEqual(user.role, models.UserRole.OFFICER)
        self.assertNotEqual(user.password_hash, "NewOfficer#2026")
        self.assertNotIn("NewOfficer#2026", user.password_hash)
        self.assertTrue(verify_password("NewOfficer#2026", user.password_hash))

        audits = [row for row in self._audit_rows() if row.action == ACTION_OFFICER_CREATED]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].entity_id, user.id)
        self.assertEqual(audits[0].actor_user_id, self.admin.id)

    def test_create_officer_ignores_a_caller_supplied_role(self):
        payload = self._officer_payload()
        payload["role"] = "admin"
        response = self._post("/api/v1/admin/officers", self.token_admin, payload)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["role"], "officer")

    def test_duplicate_email_is_rejected_without_modifying_existing_user(self):
        response = self._post(
            "/api/v1/admin/officers",
            self.token_admin,
            self._officer_payload(email=self.officer.email),
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.db.expire_all()
        existing = self.db.query(models.User).filter(models.User.id == self.officer.id).first()
        self.assertEqual(existing.password_hash, self.password_hash)

    def test_create_officer_rejects_inactive_department(self):
        response = self._post(
            "/api/v1/admin/officers",
            self.token_admin,
            self._officer_payload(department_id=self.dept_inactive.id),
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_create_officer_rejects_unsupported_city(self):
        response = self._post(
            "/api/v1/admin/officers",
            self.token_admin,
            self._officer_payload(department_id=self.dept_unsupported.id),
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_create_officer_accepts_an_existing_bengaluru_department(self):
        response = self._post(
            "/api/v1/admin/officers",
            self.token_admin,
            self._officer_payload(
                email="task10.bengaluru.officer@example.com",
                department_id=self.dept_bengaluru.id,
            ),
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["department_id"], self.dept_bengaluru.id)

    def test_department_change_is_audited_and_preserves_historical_department(self):
        response = self._patch(
            f"/api/v1/admin/officers/{self.officer.id}/department",
            self.token_admin,
            {
                "department_id": self.dept_garbage.id,
                "reason": "Operational reorganisation approved by the municipal commissioner.",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["department_id"], self.dept_garbage.id)

        self.db.expire_all()
        officer = self.db.query(models.User).filter(models.User.id == self.officer.id).first()
        self.assertEqual(officer.department_id, self.dept_garbage.id)

        historical = self.db.query(models.Issue).filter(
            models.Issue.id == self.historical_gurugram_roads.id
        ).first()
        self.assertEqual(historical.department_id, self.dept_roads.id)

        audits = [row for row in self._audit_rows() if row.action == ACTION_OFFICER_DEPARTMENT_CHANGED]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].before_state["department_id"], self.dept_roads.id)
        self.assertEqual(audits[0].after_state["department_id"], self.dept_garbage.id)

    def test_department_change_rejects_citizen_account(self):
        response = self._patch(
            f"/api/v1/admin/officers/{self.citizen.id}/department",
            self.token_admin,
            {"department_id": self.dept_roads.id, "reason": "Attempting to modify a citizen account."},
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_department_change_rejects_same_department(self):
        response = self._patch(
            f"/api/v1/admin/officers/{self.officer.id}/department",
            self.token_admin,
            {"department_id": self.dept_roads.id, "reason": "Attempting a no-op department change."},
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_department_change_rejects_unknown_officer(self):
        response = self._patch(
            "/api/v1/admin/officers/does-not-exist/department",
            self.token_admin,
            {"department_id": self.dept_roads.id, "reason": "Attempting to modify a missing officer."},
        )
        self.assertEqual(response.status_code, 404, response.text)

    def test_officer_list_never_exposes_password_hashes(self):
        response = self._get("/api/v1/admin/officers", self.token_admin)
        self.assertEqual(response.status_code, 200, response.text)
        for row in response.json():
            self.assertNotIn("password_hash", row)
            self.assertNotIn("password", row)


# ---------------------------------------------------------------------------
# E. Audit log
# ---------------------------------------------------------------------------
class TestAdminAuditLog(AdminPortalTestBase):
    def _route(self, issue_id=None, reason="Verified ownership confirmed by the official source."):
        return self._post(
            f"/api/v1/admin/issues/{issue_id or self.review_road.id}/route",
            self.token_admin,
            {"department_id": self.dept_roads.id, "reason": reason},
        )

    def test_audit_entry_has_all_required_fields(self):
        self._route()
        response = self._get("/api/v1/admin/audit-logs", self.token_admin)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["total"], 1)
        entry = body["items"][0]
        for field in (
            "id", "actor_user_id", "actor_name", "action", "entity_type",
            "entity_id", "before_state", "after_state", "reason", "city",
            "created_at",
        ):
            self.assertIn(field, entry)
        self.assertEqual(entry["actor_user_id"], self.admin.id)
        self.assertEqual(entry["entity_type"], "issue")
        self.assertEqual(entry["city"], CITY_GURUGRAM)

    def test_audit_filters_by_action_entity_actor_city_and_date(self):
        self._route()
        self._post(
            f"/api/v1/admin/issues/{self.review_nomap.id}/route",
            self.token_admin,
            {"department_id": None, "reason": "No verified mapping exists for this category yet."},
        )

        by_action = self._get(
            f"/api/v1/admin/audit-logs?action={ACTION_ISSUE_LEFT_UNASSIGNED}",
            self.token_admin,
        ).json()
        self.assertEqual(by_action["total"], 1)
        self.assertEqual(by_action["items"][0]["entity_id"], self.review_nomap.id)

        by_entity = self._get(
            f"/api/v1/admin/audit-logs?entity_type=issue&entity_id={self.review_road.id}",
            self.token_admin,
        ).json()
        self.assertEqual(by_entity["total"], 1)

        by_actor = self._get(
            f"/api/v1/admin/audit-logs?actor_user_id={self.admin.id}", self.token_admin
        ).json()
        self.assertEqual(by_actor["total"], 2)

        by_city = self._get(
            f"/api/v1/admin/audit-logs?city={CITY_GURUGRAM.replace(' ', '%20')}",
            self.token_admin,
        ).json()
        self.assertEqual(by_city["total"], 2)

        today = datetime.now(timezone.utc).date().isoformat()
        by_date = self._get(
            f"/api/v1/admin/audit-logs?date_from={today}&date_to={today}",
            self.token_admin,
        ).json()
        self.assertEqual(by_date["total"], 2)

    def test_audit_pagination_returns_stable_pages(self):
        for index in range(3):
            response = self._post(
                f"/api/v1/admin/issues/{self.review_road.id}/route",
                self.token_admin,
                {"department_id": self.dept_roads.id, "reason": f"Documented decision number {index + 1} for this complaint."},
            )
            # Only the first call can succeed; later calls are already assigned.
            if index == 0:
                self.assertEqual(response.status_code, 200, response.text)
            else:
                self.assertEqual(response.status_code, 409, response.text)

        self._post(
            f"/api/v1/admin/issues/{self.review_nomap.id}/route",
            self.token_admin,
            {"department_id": None, "reason": "Kept unassigned pending a verified mapping."},
        )

        first = self._get("/api/v1/admin/audit-logs?skip=0&limit=1", self.token_admin).json()
        second = self._get("/api/v1/admin/audit-logs?skip=1&limit=1", self.token_admin).json()
        self.assertEqual(first["total"], 2)
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(len(second["items"]), 1)
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])

    def test_audit_endpoints_are_read_only(self):
        paths = app.openapi()["paths"]
        audit_methods = set(paths["/api/v1/admin/audit-logs"].keys())
        self.assertEqual(audit_methods, {"get"})
        # No update/delete route exists anywhere under the audit namespace.
        for path, operations in paths.items():
            if path.startswith("/api/v1/admin/audit"):
                self.assertNotIn("patch", operations)
                self.assertNotIn("put", operations)
                self.assertNotIn("delete", operations)

    def test_audit_rows_are_immutable_through_the_orm(self):
        entry = record_admin_audit(
            self.db,
            actor=self.admin,
            action=ACTION_ISSUE_ROUTED,
            entity_type="issue",
            entity_id=self.review_road.id,
            before_state={"department_id": None},
            after_state={"department_id": self.dept_roads.id},
            reason="Immutable audit fixture.",
            city=CITY_GURUGRAM,
        )
        self.db.commit()

        entry.reason = "Attempted rewrite."
        with self.assertRaises(ValueError):
            self.db.commit()
        self.db.rollback()

        self.db.expire_all()
        reloaded = self.db.query(models.AdminAuditLog).filter(
            models.AdminAuditLog.id == entry.id
        ).first()
        self.assertEqual(reloaded.reason, "Immutable audit fixture.")

    def test_business_change_and_audit_commit_atomically(self):
        session = TestingSessionLocal()
        original_commit = session.commit

        def failing_commit():
            raise RuntimeError("simulated commit failure")

        try:
            issue = session.query(models.Issue).filter(
                models.Issue.id == self.review_road.id
            ).first()
            actor = session.query(models.User).filter(
                models.User.id == self.admin.id
            ).first()
            department_id = self.dept_roads.id
            issue_id = issue.id

            session.commit = failing_commit
            with self.assertRaises(RuntimeError):
                route_issue(
                    session,
                    actor=actor,
                    issue_id=issue_id,
                    department_id=department_id,
                    reason="Verified ownership confirmed by the official source.",
                )
        finally:
            session.commit = original_commit
            session.rollback()
            session.close()

        self.db.expire_all()
        issue = self.db.query(models.Issue).filter(models.Issue.id == self.review_road.id).first()
        self.assertIsNone(issue.department_id)
        self.assertEqual(issue.routing_status, ROUTING_STATUS_UNASSIGNED_REVIEW)
        self.assertEqual(self.db.query(models.AdminAuditLog).count(), 0)

    def test_audit_payload_contains_no_credentials_or_tokens(self):
        self._route()
        response = self._get("/api/v1/admin/audit-logs", self.token_admin)
        serialized = json.dumps(response.json()).lower()
        for forbidden in ("password", "password_hash", "bearer", "authorization", "access_token", "secret"):
            self.assertNotIn(forbidden, serialized)


# ---------------------------------------------------------------------------
# F. Frontend and portal routes
# ---------------------------------------------------------------------------
class TestAdminPortalFrontend(AdminPortalTestBase):
    def test_admin_route_serves_the_portal_page(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn("UIMRS Administrator Portal", body)
        self.assertIn("/api/v1", body)
        self.assertIn("admin/overview", body)
        self.assertIn("admin/review-queue", body)
        self.assertIn("admin/audit-logs", body)

    def test_existing_portals_are_unchanged(self):
        admin = self.client.get("/admin")
        officer = self.client.get("/officer")
        citizen = self.client.get("/citizen")
        root = self.client.get("/")
        self.assertEqual(admin.status_code, 200)
        self.assertEqual(officer.status_code, 200)
        self.assertEqual(citizen.status_code, 200)
        self.assertEqual(root.status_code, 200)
        self.assertNotEqual(admin.text, officer.text)
        self.assertNotEqual(admin.text, citizen.text)
        self.assertIn("Officer", officer.text)
        self.assertIn("Citizen", citizen.text)

    def test_admin_page_uses_safe_dom_and_session_storage_only(self):
        body = ADMIN_PAGE.read_text(encoding="utf-8")
        self.assertIn("textContent", body)
        self.assertIn("replaceChildren", body)
        self.assertIn("createElement", body)
        self.assertIn("sessionStorage", body)
        self.assertNotIn("localStorage", body)
        self.assertNotIn(".innerHTML", body)
        self.assertNotIn("document.write", body)
        self.assertNotIn("eval(", body)

    def test_logout_clears_the_token_and_cached_state(self):
        body = ADMIN_PAGE.read_text(encoding="utf-8")
        self.assertIn("sessionStorage.removeItem", body)
        self.assertIn("resetSessionState", body)
        # The token key is never written to local storage.
        self.assertNotIn("localStorage.setItem", body)

    def test_admin_page_never_sends_an_actor_or_role_override(self):
        body = ADMIN_PAGE.read_text(encoding="utf-8")
        # Business payloads contain only the chosen department and justification.
        self.assertIn(
            "const payload = { department_id: departmentId, reason: justification };",
            body,
        )
        # The officer-creation payload never includes a role or actor field.
        officer_payload = body.split("const payload = {", 2)[2].split("};", 1)[0]
        self.assertNotIn("role", officer_payload)
        self.assertNotIn("actor", officer_payload)
        self.assertNotIn("actor_id", body)

    def test_admin_page_handles_expired_sessions(self):
        body = ADMIN_PAGE.read_text(encoding="utf-8")
        self.assertIn("handleAuthExpired", body)
        self.assertIn("status === 401", body)


# ---------------------------------------------------------------------------
# G. Schema guard and migration
# ---------------------------------------------------------------------------
class TestAdminSchemaAndMigration(unittest.TestCase):
    def test_head_revision_and_guard_include_the_audit_log(self):
        self.assertEqual(HEAD_REVISION, "0005_image_data_bytea")
        self.assertIn(AUDIT_LOG_TABLE, REQUIRED_SCHEMA)
        self.assertIn("actor_user_id", REQUIRED_SCHEMA[AUDIT_LOG_TABLE])
        self.assertIn("before_state", REQUIRED_SCHEMA[AUDIT_LOG_TABLE])
        self.assertIn("after_state", REQUIRED_SCHEMA[AUDIT_LOG_TABLE])
        self.assertIn("created_at", REQUIRED_SCHEMA[AUDIT_LOG_TABLE])

    def test_empty_database_migrates_to_head_with_audit_table(self):
        temp_dir = tempfile.mkdtemp(prefix="uimrs_admin_migration_")
        db_path = Path(temp_dir) / "empty.db"
        url = f"sqlite:///{db_path.as_posix()}"
        try:
            result = prepare_database(url)
            self.assertEqual(result.revision, HEAD_REVISION)
            self.assertEqual(current_revision(url), HEAD_REVISION)
            con = sqlite3.connect(str(db_path))
            try:
                tables = {
                    row[0] for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                indexes = {
                    row[0] for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='index' "
                        "AND tbl_name='admin_audit_logs'"
                    )
                }
            finally:
                con.close()
            self.assertIn(AUDIT_LOG_TABLE, tables)
            self.assertIn("ix_admin_audit_logs_action", indexes)
            self.assertIn("ix_admin_audit_logs_entity_type", indexes)
            self.assertIn("ix_admin_audit_logs_created_at", indexes)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
