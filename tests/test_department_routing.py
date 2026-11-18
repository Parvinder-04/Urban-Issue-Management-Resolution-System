"""UIMRS Task 8 — verified department directory and city-aware routing.

Covers:
A. Department directory invariants (verification provenance, contacts, modes).
B. City separation: no category-only cross-city fallback.
C. Routing outcomes: automatic / manual review / disabled / missing.
D. Historical Bengaluru preservation and officer-queue exclusion.
E. Issue-creation regression (auth, ownership, classification, duplicates).
F. Controlled migration on a disposable copy of the pre-change schema.

All write tests run against an isolated in-memory SQLite database. Migration
tests use a temporary file database. The actual development database is only
ever read, never written, by this module.
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.db_guard import (
    DEPARTMENT_ROUTING_COLUMNS,
    ISSUE_ROUTING_COLUMNS,
    DatabaseSchemaError,
    guard_startup,
)
from app.db_migrate import (
    BASELINE_REVISION,
    HEAD_REVISION,
    current_revision,
    make_alembic_config,
    prepare_database,
)
from app.main import app
from app.routers.auth import create_access_token, hash_password
from app.services.classification_service import (
    CITY_BENGALURU,
    CITY_GURUGRAM,
    ClassificationResult,
    PriorityResult,
)
from app.services.department_routing import (
    ROUTING_MODE_AUTOMATIC,
    ROUTING_MODE_DISABLED,
    ROUTING_MODE_MANUAL_REVIEW,
    ROUTING_STATUS_ASSIGNED,
    ROUTING_STATUS_UNASSIGNED_NO_MAPPING,
    ROUTING_STATUS_UNASSIGNED_REVIEW,
    resolve_department,
)

DEV_DB = PROJECT_ROOT / "uimrs_india_dev.db"
TEST_PASSWORD = "Task8Routing#2026"
VERIFIED_SOURCE = "https://mcg.gov.in/contact/office-contacts"
VERIFIED_DATE = "2026-11-14"

# Isolated in-memory database shared by the API/unit tests in this module.
engine = create_engine(
    "sqlite:///:memory:",
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


def make_department(**overrides):
    values = {
        "name": "TEST_TASK8_DEPARTMENT",
        "handles_category": models.IssueCategory.GARBAGE_WASTE,
        "city": CITY_GURUGRAM,
        "source_agency": "TEST_TASK8",
        "routing_mode": ROUTING_MODE_MANUAL_REVIEW,
        "is_active": True,
    }
    values.update(overrides)
    return models.Department(**values)


def clear_database(db):
    db.query(models.IssueStatusUpdate).delete()
    db.query(models.IssueImage).delete()
    db.query(models.Issue).delete()
    db.query(models.User).delete()
    db.query(models.Department).delete()
    db.commit()


class DirectoryTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)

    def setUp(self):
        self.db = TestingSessionLocal()
        clear_database(self.db)

    def tearDown(self):
        self.db.close()


# ---------------------------------------------------------------------------
# A. Department directory
# ---------------------------------------------------------------------------
class TestDepartmentDirectory(DirectoryTestBase):
    def test_verified_automatic_mapping_has_source_and_date(self):
        department = make_department(
            routing_mode=ROUTING_MODE_AUTOMATIC,
            verification_source_url=VERIFIED_SOURCE,
            verification_date=VERIFIED_DATE,
            contact_phone="18001801817",
            contact_email="info@mcg.gov.in",
            contact_purpose="MCG 24x7 municipal complaint helpline",
            jurisdiction="Municipal Corporation of Gurugram area",
        )
        self.db.add(department)
        self.db.commit()

        stored = self.db.query(models.Department).one()
        self.assertEqual(stored.verification_source_url, VERIFIED_SOURCE)
        self.assertEqual(stored.verification_date, VERIFIED_DATE)
        self.assertEqual(stored.routing_mode, ROUTING_MODE_AUTOMATIC)
        self.assertTrue(stored.is_active)

    def test_missing_contact_data_remains_null(self):
        department = make_department()
        self.db.add(department)
        self.db.commit()

        stored = self.db.query(models.Department).one()
        self.assertIsNone(stored.contact_email)
        self.assertIsNone(stored.contact_phone)
        self.assertIsNone(stored.contact_purpose)
        self.assertIsNone(stored.verification_source_url)

    def test_unverified_automatic_mapping_is_not_trusted(self):
        # An automatic row without provenance must be downgraded, never assigned.
        self.db.add(make_department(routing_mode=ROUTING_MODE_AUTOMATIC))
        self.db.commit()

        decision = resolve_department(
            self.db, CITY_GURUGRAM, models.IssueCategory.GARBAGE_WASTE
        )
        self.assertIsNone(decision.department)
        self.assertEqual(decision.status, ROUTING_STATUS_UNASSIGNED_REVIEW)
        self.assertIn("verification", decision.reason.lower())

    def test_city_and_category_combination_is_unambiguous(self):
        self.db.add_all(
            [
                make_department(
                    name="TEST_TASK8_GURUGRAM_GARBAGE",
                    handles_category=models.IssueCategory.GARBAGE_WASTE,
                    city=CITY_GURUGRAM,
                    routing_mode=ROUTING_MODE_AUTOMATIC,
                    verification_source_url=VERIFIED_SOURCE,
                    verification_date=VERIFIED_DATE,
                ),
                make_department(
                    name="TEST_TASK8_BENGALURU_GARBAGE",
                    handles_category=models.IssueCategory.GARBAGE_WASTE,
                    city=CITY_BENGALURU,
                    routing_mode=ROUTING_MODE_MANUAL_REVIEW,
                ),
            ]
        )
        self.db.commit()

        gurugram = resolve_department(
            self.db, CITY_GURUGRAM, models.IssueCategory.GARBAGE_WASTE
        )
        bengaluru = resolve_department(
            self.db, CITY_BENGALURU, models.IssueCategory.GARBAGE_WASTE
        )

        self.assertEqual(gurugram.department.name, "TEST_TASK8_GURUGRAM_GARBAGE")
        self.assertIsNone(bengaluru.department)
        self.assertEqual(bengaluru.status, ROUTING_STATUS_UNASSIGNED_REVIEW)


# ---------------------------------------------------------------------------
# B. City separation
# ---------------------------------------------------------------------------
class TestCitySeparation(DirectoryTestBase):
    def test_each_city_only_uses_its_own_mapping(self):
        gurugram = make_department(
            name="TEST_TASK8_GURUGRAM",
            city=CITY_GURUGRAM,
            routing_mode=ROUTING_MODE_AUTOMATIC,
            verification_source_url=VERIFIED_SOURCE,
            verification_date=VERIFIED_DATE,
        )
        bengaluru = make_department(
            name="TEST_TASK8_BENGALURU",
            city=CITY_BENGALURU,
            routing_mode=ROUTING_MODE_AUTOMATIC,
            verification_source_url=VERIFIED_SOURCE,
            verification_date=VERIFIED_DATE,
        )
        self.db.add_all([gurugram, bengaluru])
        self.db.commit()

        self.assertEqual(
            resolve_department(
                self.db, CITY_GURUGRAM, models.IssueCategory.GARBAGE_WASTE
            ).department.id,
            gurugram.id,
        )
        self.assertEqual(
            resolve_department(
                self.db, CITY_BENGALURU, models.IssueCategory.GARBAGE_WASTE
            ).department.id,
            bengaluru.id,
        )

    def test_unknown_city_cannot_use_another_citys_department(self):
        self.db.add(
            make_department(
                city=CITY_GURUGRAM,
                routing_mode=ROUTING_MODE_AUTOMATIC,
                verification_source_url=VERIFIED_SOURCE,
                verification_date=VERIFIED_DATE,
            )
        )
        self.db.commit()

        decision = resolve_department(
            self.db, "Mumbai, Maharashtra, India", models.IssueCategory.GARBAGE_WASTE
        )
        self.assertIsNone(decision.department)
        self.assertEqual(decision.status, ROUTING_STATUS_UNASSIGNED_NO_MAPPING)

    def test_missing_city_is_never_category_only_matched(self):
        self.db.add(
            make_department(
                city=CITY_GURUGRAM,
                routing_mode=ROUTING_MODE_AUTOMATIC,
                verification_source_url=VERIFIED_SOURCE,
                verification_date=VERIFIED_DATE,
            )
        )
        self.db.commit()

        decision = resolve_department(
            self.db, None, models.IssueCategory.GARBAGE_WASTE
        )
        self.assertIsNone(decision.department)
        self.assertEqual(decision.status, ROUTING_STATUS_UNASSIGNED_NO_MAPPING)

    def test_gurugram_never_resolves_to_bengaluru_department(self):
        self.db.add(
            make_department(
                name="TEST_TASK8_BENGALURU_ONLY",
                city=CITY_BENGALURU,
                routing_mode=ROUTING_MODE_AUTOMATIC,
                verification_source_url=VERIFIED_SOURCE,
                verification_date=VERIFIED_DATE,
            )
        )
        self.db.commit()

        decision = resolve_department(
            self.db, CITY_GURUGRAM, models.IssueCategory.GARBAGE_WASTE
        )
        self.assertIsNone(decision.department)
        self.assertEqual(decision.status, ROUTING_STATUS_UNASSIGNED_NO_MAPPING)


# ---------------------------------------------------------------------------
# C. Routing decisions
# ---------------------------------------------------------------------------
class TestRoutingDecisions(DirectoryTestBase):
    def test_automatic_mapping_assigns(self):
        department = make_department(
            routing_mode=ROUTING_MODE_AUTOMATIC,
            verification_source_url=VERIFIED_SOURCE,
            verification_date=VERIFIED_DATE,
        )
        self.db.add(department)
        self.db.commit()

        decision = resolve_department(
            self.db, CITY_GURUGRAM, models.IssueCategory.GARBAGE_WASTE
        )
        self.assertEqual(decision.status, ROUTING_STATUS_ASSIGNED)
        self.assertEqual(decision.department.id, department.id)
        self.assertIn(VERIFIED_SOURCE, decision.reason)

    def test_manual_review_stays_unassigned(self):
        self.db.add(make_department(routing_mode=ROUTING_MODE_MANUAL_REVIEW))
        self.db.commit()

        decision = resolve_department(
            self.db, CITY_GURUGRAM, models.IssueCategory.GARBAGE_WASTE
        )
        self.assertIsNone(decision.department)
        self.assertEqual(decision.status, ROUTING_STATUS_UNASSIGNED_REVIEW)
        self.assertIn("jurisdiction review", decision.reason)

    def test_disabled_mapping_is_unassigned_no_mapping(self):
        self.db.add(make_department(routing_mode=ROUTING_MODE_DISABLED))
        self.db.commit()

        decision = resolve_department(
            self.db, CITY_GURUGRAM, models.IssueCategory.GARBAGE_WASTE
        )
        self.assertIsNone(decision.department)
        self.assertEqual(decision.status, ROUTING_STATUS_UNASSIGNED_NO_MAPPING)

    def test_missing_mapping_is_unassigned_no_mapping(self):
        decision = resolve_department(
            self.db, CITY_GURUGRAM, models.IssueCategory.STREETLIGHT
        )
        self.assertIsNone(decision.department)
        self.assertEqual(decision.status, ROUTING_STATUS_UNASSIGNED_NO_MAPPING)

    def test_inactive_mapping_is_not_used(self):
        self.db.add(
            make_department(
                routing_mode=ROUTING_MODE_AUTOMATIC,
                verification_source_url=VERIFIED_SOURCE,
                verification_date=VERIFIED_DATE,
                is_active=False,
            )
        )
        self.db.commit()

        decision = resolve_department(
            self.db, CITY_GURUGRAM, models.IssueCategory.GARBAGE_WASTE
        )
        self.assertIsNone(decision.department)
        self.assertEqual(decision.status, ROUTING_STATUS_UNASSIGNED_NO_MAPPING)


# ---------------------------------------------------------------------------
# E. Issue creation regression
# ---------------------------------------------------------------------------
class TestIssueCreationRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.password_hash = hash_password(TEST_PASSWORD)

    def setUp(self):
        self.db = TestingSessionLocal()
        self.old_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_db] = override_get_db
        clear_database(self.db)

        self.citizen = models.User(
            name="TEST_TASK8_CITIZEN",
            email="task8.citizen@uimrs.test",
            password_hash=self.password_hash,
            role=models.UserRole.CITIZEN,
        )
        self.admin = models.User(
            name="TEST_TASK8_ADMIN",
            email="task8.admin@uimrs.test",
            password_hash=self.password_hash,
            role=models.UserRole.ADMIN,
        )
        self.db.add_all([self.citizen, self.admin])
        self.db.commit()
        self.token = create_access_token(self.citizen)

    def tearDown(self):
        self.db.close()
        app.dependency_overrides = self.old_overrides

    def _post(self, payload, token=None):
        return self.client.post(
            "/api/v1/issues",
            json=payload,
            headers=bearer(token if token is not None else self.token),
        )

    def _payload(self, city="Gurugram, Haryana, India"):
        if "Bengaluru" in city:
            return {
                "title": "Garbage dumped near the market",
                "description": "Garbage has been dumped near the market for days.",
                "city": city,
                "latitude": 12.9716,
                "longitude": 77.5946,
            }
        return {
            "title": "Garbage dumped near the market",
            "description": "Garbage has been dumped near the market for days.",
            "city": city,
            "latitude": 28.4601,
            "longitude": 77.0648,
        }

    def _classify_as(self, category):
        return patch(
            "app.routers.issues.classifier.classify",
            return_value=ClassificationResult(
                category=category, confidence=0.9, source="india_keyword_rule"
            ),
        )

    def _score_priority(self):
        return patch(
            "app.routers.issues.priority_engine.score",
            return_value=PriorityResult(score=5.0, level="Medium", reasons=["test"]),
        )

    def test_automatic_mapping_assigns_and_stores_reason(self):
        department = make_department(
            routing_mode=ROUTING_MODE_AUTOMATIC,
            verification_source_url=VERIFIED_SOURCE,
            verification_date=VERIFIED_DATE,
        )
        self.db.add(department)
        self.db.commit()

        with self._classify_as(models.IssueCategory.GARBAGE_WASTE), self._score_priority():
            response = self._post(self._payload())

        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["department_id"], department.id)
        self.assertEqual(data["routing_status"], ROUTING_STATUS_ASSIGNED)
        self.assertIn(VERIFIED_SOURCE, data["routing_reason"])

    def test_manual_review_issue_stays_unassigned(self):
        self.db.add(
            make_department(
                handles_category=models.IssueCategory.ROAD_DAMAGE,
                routing_mode=ROUTING_MODE_MANUAL_REVIEW,
            )
        )
        self.db.commit()

        with self._classify_as(models.IssueCategory.ROAD_DAMAGE), self._score_priority():
            response = self._post(self._payload())

        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertIsNone(data["department_id"])
        self.assertEqual(data["routing_status"], ROUTING_STATUS_UNASSIGNED_REVIEW)
        self.assertTrue(data["routing_reason"])

    def test_missing_mapping_issue_is_explicitly_unassigned(self):
        with self._classify_as(models.IssueCategory.STREETLIGHT), self._score_priority():
            response = self._post(self._payload())

        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertIsNone(data["department_id"])
        self.assertEqual(data["routing_status"], ROUTING_STATUS_UNASSIGNED_NO_MAPPING)
        self.assertIn("No active verified department mapping", data["routing_reason"])

    def test_bengaluru_only_mapping_is_not_used_for_gurugram(self):
        self.db.add(
            make_department(
                name="TEST_TASK8_BENGALURU_GARBAGE",
                city=CITY_BENGALURU,
                routing_mode=ROUTING_MODE_AUTOMATIC,
                verification_source_url=VERIFIED_SOURCE,
                verification_date=VERIFIED_DATE,
            )
        )
        self.db.commit()

        with self._classify_as(models.IssueCategory.GARBAGE_WASTE), self._score_priority():
            response = self._post(self._payload(city="Gurugram, Haryana, India"))

        self.assertEqual(response.status_code, 201, response.text)
        self.assertIsNone(response.json()["department_id"])
        self.assertEqual(
            response.json()["routing_status"], ROUTING_STATUS_UNASSIGNED_NO_MAPPING
        )

    def test_authentication_and_ownership_preserved(self):
        with self._classify_as(models.IssueCategory.GARBAGE_WASTE), self._score_priority():
            anonymous = self.client.post("/api/v1/issues", json=self._payload())
        self.assertEqual(anonymous.status_code, 401)

        with self._classify_as(models.IssueCategory.GARBAGE_WASTE), self._score_priority():
            response = self._post(self._payload())
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["reporter_id"], self.citizen.id)

    def test_duplicate_detection_still_works(self):
        with self._classify_as(models.IssueCategory.GARBAGE_WASTE), self._score_priority():
            first = self._post(self._payload())
            second = self._post(self._payload())

        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(second.json()["status"], "duplicate")


# ---------------------------------------------------------------------------
# D. Historical preservation
# ---------------------------------------------------------------------------
class TestHistoricalPreservation(DirectoryTestBase):
    def test_historical_row_keeps_department_and_provenance(self):
        bengaluru_department = make_department(
            name="TEST_TASK8_BBMP",
            city=CITY_BENGALURU,
            source_agency="BBMP",
            routing_mode=ROUTING_MODE_MANUAL_REVIEW,
        )
        self.db.add(bengaluru_department)
        self.db.commit()

        reporter = models.User(
            name="TEST_TASK8_HIST_REPORTER",
            email="task8.hist@uimrs.test",
            password_hash=hash_password(TEST_PASSWORD),
            role=models.UserRole.ADMIN,
        )
        self.db.add(reporter)
        self.db.commit()

        historical = models.Issue(
            ticket_number="TEST-TASK8-HIST-1",
            title="Historical Bengaluru fixture",
            description="Isolated historical fixture, not a real complaint.",
            category=models.IssueCategory.GARBAGE_WASTE,
            status=models.IssueStatus.REPORTED,
            latitude=12.9716,
            longitude=77.5946,
            reporter_id=reporter.id,
            department_id=bengaluru_department.id,
            city=CITY_BENGALURU,
            source_agency="BBMP",
            source_record_id="TEST-HIST-1",
            is_historical=True,
            priority_score=None,
            priority_level=None,
        )
        self.db.add(historical)
        self.db.commit()
        # Imported historical rows genuinely carry NULL priority/explainability.
        # SQLAlchemy applies column defaults on INSERT, so force the real
        # historical state explicitly for this fixture.
        self.db.query(models.Issue).filter(models.Issue.id == historical.id).update(
            {
                models.Issue.priority_score: None,
                models.Issue.priority_level: None,
            }
        )
        self.db.commit()
        self.db.refresh(historical)

        stored = self.db.query(models.Issue).filter(
            models.Issue.id == historical.id
        ).one()
        self.assertEqual(stored.department_id, bengaluru_department.id)
        self.assertEqual(stored.source_agency, "BBMP")
        self.assertIsNone(stored.priority_score)
        self.assertIsNone(stored.category_model_version)
        self.assertIsNone(stored.routing_status)

    def test_historical_rows_are_never_actionable_live_complaints(self):
        department = make_department(
            name="TEST_TASK8_OFFICER_DEPT",
            city=CITY_BENGALURU,
            routing_mode=ROUTING_MODE_MANUAL_REVIEW,
        )
        self.db.add(department)
        self.db.commit()

        admin = models.User(
            name="TEST_TASK8_ADMIN2",
            email="task8.admin2@uimrs.test",
            password_hash=hash_password(TEST_PASSWORD),
            role=models.UserRole.ADMIN,
        )
        officer = models.User(
            name="TEST_TASK8_OFFICER",
            email="task8.officer@uimrs.test",
            password_hash=hash_password(TEST_PASSWORD),
            role=models.UserRole.OFFICER,
            department_id=department.id,
        )
        self.db.add_all([admin, officer])
        self.db.commit()

        live = models.Issue(
            ticket_number="TEST-TASK8-LIVE-1",
            title="Live officer queue fixture",
            description="Isolated live fixture, not a real complaint.",
            category=models.IssueCategory.GARBAGE_WASTE,
            status=models.IssueStatus.REPORTED,
            latitude=12.9716,
            longitude=77.5946,
            reporter_id=admin.id,
            department_id=department.id,
            city=CITY_BENGALURU,
            is_historical=False,
            assigned_officer_id=officer.id,
            routing_status=ROUTING_STATUS_ASSIGNED,
        )
        historical = models.Issue(
            ticket_number="TEST-TASK8-HIST-2",
            title="Historical officer queue fixture",
            description="Isolated historical fixture, not a real complaint.",
            category=models.IssueCategory.GARBAGE_WASTE,
            status=models.IssueStatus.REPORTED,
            latitude=12.9716,
            longitude=77.5946,
            reporter_id=admin.id,
            department_id=department.id,
            city=CITY_BENGALURU,
            is_historical=True,
        )
        self.db.add_all([live, historical])
        self.db.commit()

        # The data-level guarantee: historical provenance is never routed and
        # never carries a live officer assignment.
        self.assertTrue(historical.is_historical)
        self.assertIsNone(historical.routing_status)
        self.assertIsNone(historical.routing_reason)
        self.assertIsNone(historical.assigned_officer_id)

        client = TestClient(app)
        old_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_db] = override_get_db
        try:
            historical_update = client.patch(
                f"/api/v1/issues/{historical.id}/status",
                json={"new_status": "in_progress"},
                headers=bearer(create_access_token(officer)),
            )
            live_update = client.patch(
                f"/api/v1/issues/{live.id}/status",
                json={"new_status": "in_progress", "comment": "Field crew assigned."},
                headers=bearer(create_access_token(officer)),
            )
        finally:
            app.dependency_overrides = old_overrides

        # Historical records remain immutable; the live complaint is actionable.
        self.assertEqual(historical_update.status_code, 403)
        self.assertEqual(live_update.status_code, 200, live_update.text)


# ---------------------------------------------------------------------------
# Live directory invariants (read-only)
# ---------------------------------------------------------------------------
class TestLiveDirectoryReadOnly(unittest.TestCase):
    """Read-only checks against the real development directory.

    These never write to the database and are skipped only when the development
    database is not present (for example on a clean checkout).
    """

    def setUp(self):
        if not DEV_DB.exists():
            self.skipTest("development database not present")
        connection = sqlite3.connect(
            f"file:{DEV_DB.as_posix()}?mode=ro", uri=True
        )
        try:
            has_directory = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'departments'"
            ).fetchone()
        finally:
            connection.close()
        if not has_directory:
            # A clean checkout (or the team package) has no operational
            # development database, and another test may leave an empty file
            # behind. Skip instead of failing on a database that was never
            # provisioned.
            self.skipTest("development database has no department directory")
        self.connection = sqlite3.connect(
            f"file:{DEV_DB.as_posix()}?mode=ro", uri=True
        )
        self.addCleanup(self.connection.close)

    def test_automatic_rows_carry_verification_provenance(self):
        rows = self.connection.execute(
            "SELECT name, verification_source_url, verification_date "
            "FROM departments WHERE routing_mode = 'automatic'"
        ).fetchall()
        self.assertTrue(rows, "expected at least one verified automatic mapping")
        for name, source, checked in rows:
            self.assertTrue(source, f"automatic row {name!r} has no source URL")
            self.assertTrue(checked, f"automatic row {name!r} has no verification date")

    def test_contacts_have_a_documented_purpose(self):
        rows = self.connection.execute(
            "SELECT name, contact_email, contact_phone, contact_purpose "
            "FROM departments "
            "WHERE city = ? AND (contact_email IS NOT NULL OR contact_phone IS NOT NULL)",
            (CITY_GURUGRAM,),
        ).fetchall()
        self.assertTrue(rows, "expected verified Gurugram contacts")
        for name, email, phone, purpose in rows:
            self.assertTrue(
                purpose, f"contact for {name!r} has no documented purpose"
            )
            self.assertTrue(
                (email or "").endswith((".gov.in", ".nic.in", "gmail.com"))
                or email is None,
                f"unexpected contact email for {name!r}: {email!r}",
            )

    def test_directory_has_no_cross_city_rows(self):
        cities = {
            row[0]
            for row in self.connection.execute(
                "SELECT DISTINCT city FROM departments WHERE city IS NOT NULL"
            )
        }
        self.assertTrue(cities.issubset({CITY_GURUGRAM, CITY_BENGALURU}), cities)


# ---------------------------------------------------------------------------
# F. Migration
# ---------------------------------------------------------------------------
def sqlite_url(path: Path) -> str:
    return f"sqlite:///{Path(path).as_posix()}"


def _issue_columns(path: Path) -> set[str]:
    connection = sqlite3.connect(str(path))
    try:
        return {
            row[1] for row in connection.execute("PRAGMA table_info('issues')")
        }
    finally:
        connection.close()


def _department_columns(path: Path) -> set[str]:
    connection = sqlite3.connect(str(path))
    try:
        return {
            row[1] for row in connection.execute("PRAGMA table_info('departments')")
        }
    finally:
        connection.close()


class TestDepartmentRoutingMigration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "task8_migration.db"
        self.url = sqlite_url(self.db_path)

    def _create_baseline(self):
        command.upgrade(make_alembic_config(self.url), BASELINE_REVISION)

    def _seed_baseline_rows(self):
        connection = sqlite3.connect(str(self.db_path))
        try:
            connection.execute(
                "INSERT INTO users (id, name, email, password_hash, role, "
                "preferred_language) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "user-hist-1",
                    "Historical Fixture",
                    "hist.fixture@uimrs.test",
                    "not-a-real-hash",
                    "ADMIN",
                    "en",
                ),
            )
            connection.execute(
                "INSERT INTO departments (id, name, handles_category, contact_email, "
                "contact_phone, city, source_agency) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "dept-bbmp-1",
                    "Bruhat Bengaluru Mahanagara Palike (BBMP)",
                    "GARBAGE_WASTE",
                    None,
                    None,
                    CITY_BENGALURU,
                    "BBMP",
                ),
            )
            connection.execute(
                "INSERT INTO departments (id, name, handles_category, contact_email, "
                "contact_phone, city, source_agency) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "dept-gurugram-1",
                    "Municipal Corporation of Gurugram (MCG)",
                    "GARBAGE_WASTE",
                    None,
                    None,
                    CITY_GURUGRAM,
                    "MCG",
                ),
            )
            connection.execute(
                "INSERT INTO issues (id, ticket_number, title, description, category, "
                "category_confidence, category_source, priority_score, priority_level, "
                "status, latitude, longitude, reporter_id, department_id, city, "
                "source_agency, source_record_id, is_historical) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "issue-hist-1",
                    "UIMRS-HIST-00001",
                    "Historical fixture",
                    "Historical fixture used only by the migration test.",
                    "GARBAGE_WASTE",
                    0.0,
                    "historical_import",
                    None,
                    None,
                    "REPORTED",
                    12.9716,
                    77.5946,
                    "user-hist-1",
                    "dept-bbmp-1",
                    CITY_BENGALURU,
                    "BBMP",
                    "SRC-HIST-1",
                    1,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _snapshot(self):
        connection = sqlite3.connect(str(self.db_path))
        try:
            departments = connection.execute(
                "SELECT id, name, city, source_agency FROM departments ORDER BY id"
            ).fetchall()
            issues = connection.execute(
                "SELECT id, department_id, source_agency, priority_score, "
                "priority_level, city, is_historical FROM issues ORDER BY id"
            ).fetchall()
        finally:
            connection.close()
        return departments, issues

    def test_upgrade_preserves_rows_and_adds_routing_columns(self):
        self._create_baseline()
        self._seed_baseline_rows()
        before = self._snapshot()

        result = prepare_database(self.url)

        self.assertEqual(result.revision, HEAD_REVISION)
        self.assertEqual(current_revision(self.url), HEAD_REVISION)
        self.assertTrue(
            set(DEPARTMENT_ROUTING_COLUMNS).issubset(_department_columns(self.db_path))
        )
        self.assertTrue(
            set(ISSUE_ROUTING_COLUMNS).issubset(_issue_columns(self.db_path))
        )
        self.assertEqual(self._snapshot(), before)

        connection = sqlite3.connect(str(self.db_path))
        try:
            modes = connection.execute(
                "SELECT DISTINCT routing_mode FROM departments"
            ).fetchall()
            active = connection.execute(
                "SELECT DISTINCT is_active FROM departments"
            ).fetchall()
            routing_status = connection.execute(
                "SELECT routing_status FROM issues WHERE id = 'issue-hist-1'"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(modes, [("manual_review",)])
        self.assertEqual(active, [(1,)])
        self.assertIsNone(routing_status)

    def test_startup_guard_accepts_migrated_schema(self):
        self._create_baseline()
        self._seed_baseline_rows()
        prepare_database(self.url)

        migrated_engine = create_engine(self.url)
        self.addCleanup(migrated_engine.dispose)
        self.assertEqual(guard_startup(migrated_engine, self.url), "validated")

    def test_second_upgrade_is_a_no_op(self):
        self._create_baseline()
        self._seed_baseline_rows()
        prepare_database(self.url)
        before = self._snapshot()

        result = prepare_database(self.url)

        self.assertEqual(result.state, "already_current")
        self.assertEqual(result.revision, HEAD_REVISION)
        self.assertEqual(self._snapshot(), before)

    def test_incompatible_database_is_refused(self):
        connection = sqlite3.connect(str(self.db_path))
        try:
            connection.execute("CREATE TABLE users (id TEXT)")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(DatabaseSchemaError):
            prepare_database(self.url)


if __name__ == "__main__":
    unittest.main()
