"""
UIMRS Task 6 — Alembic migrations and persistent AI explainability.

Verifies:
A. Alembic upgrades a pre-Task-6 schema without recreating tables, is safe to
   re-run, initialises a brand-new empty database, and downgrades only on
   disposable databases.
B. Classification explanations are persisted with honest score semantics:
   Bengaluru model version / uncalibrated score / real alternatives, Gurugram
   rule heuristics with no Bengaluru model version, and real clarification
   reasons.
C. Priority reasons produced by the real PriorityEngine are persisted; no
   reasons are invented when the engine returns none.
D. Historical rows keep every new explainability column NULL and retain their
   original provenance, priority and resolution values.
E. Authenticated issue responses expose the explanations while the public
   historical dashboard does not, and existing read-scoping rules still hold.
F. The startup schema guard accepts a migrated database and rejects one that is
   missing the new columns.

Every test uses temporary or isolated in-memory SQLite databases. The real
project databases are never opened or modified.
"""
from __future__ import annotations

import hashlib
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
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.db_guard import (
    BASELINE_SCHEMA,
    EXPLAINABILITY_COLUMNS,
    DatabaseSchemaError,
    guard_startup,
    sqlite_path_from_url,
    validate_schema,
)
from app.db_migrate import (
    BASELINE_REVISION,
    HEAD_REVISION,
    MigrationError,
    current_revision,
    make_alembic_config,
    prepare_database,
)
from app.main import app
from app.routers.auth import create_access_token, hash_password
from app.services.classification_service import (
    BENGALURU_MODEL_VERSION,
    CITY_BENGALURU,
    CITY_GURUGRAM,
    ClassificationResult,
    IssueClassifier,
    PriorityEngine,
    PriorityResult,
    score_kind_for_source,
)

# ---------------------------------------------------------------------------
# Isolated API database (in-memory, shared connection)
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


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------
def sqlite_url(path: Path) -> str:
    return f"sqlite:///{Path(path).as_posix()}"


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path))


def _create_baseline_database(url: str, *, drop_alembic_version: bool = True) -> Path:
    """Create the exact pre-Task-6 schema through revision 0001."""
    config = make_alembic_config(url)
    command.upgrade(config, BASELINE_REVISION)
    path = sqlite_path_from_url(url)
    assert path is not None
    if drop_alembic_version:
        con = _connect(path)
        try:
            con.execute("DROP TABLE alembic_version")
            con.commit()
        finally:
            con.close()
    return path


def _seed_historical_row(path: Path) -> dict:
    """Insert one historical row with real-looking provenance (isolated test data)."""
    provenance = {
        "city": "Bengaluru, Karnataka, India",
        "source_dataset": "BBMP_GRIEVANCE",
        "source_record_id": "SRC-TASK6-0001",
        "source_status": "closed",
        "source_agency": "BBMP",
        "source_category": "Road_Damage",
        "source_subcategory": "Pothole",
    }
    con = _connect(path)
    try:
        con.execute(
            "INSERT INTO users (id, name, email, password_hash, role) "
            "VALUES (?, ?, ?, ?, ?)",
            ("hist-user-1", "Historical Fixture", "hist.fixture@example.test", "x", "citizen"),
        )
        con.execute(
            "INSERT INTO issues ("
            "id, ticket_number, title, description, category, status, "
            "latitude, longitude, reporter_id, is_historical, "
            "city, source_dataset, source_record_id, source_status, "
            "source_agency, source_category, source_subcategory, "
            "priority_score, priority_level, resolved_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "hist-issue-1",
                "HIST-TASK6-0001",
                "Historical fixture title",
                "Isolated historical fixture used only by tests.",
                "Road_Damage",
                "reported",
                12.9716,
                77.5946,
                "hist-user-1",
                1,
                provenance["city"],
                provenance["source_dataset"],
                provenance["source_record_id"],
                provenance["source_status"],
                provenance["source_agency"],
                provenance["source_category"],
                provenance["source_subcategory"],
                None,
                None,
                None,
            ),
        )
        con.commit()
    finally:
        con.close()
    return provenance


def _baseline_columns() -> list[str]:
    return list(BASELINE_SCHEMA["issues"])


def _issue_digest(path: Path) -> str:
    """Hash every pre-Task-6 issue column so provenance changes are detectable."""
    columns = ", ".join(_baseline_columns())
    con = _connect(path)
    try:
        digest = hashlib.sha256()
        for row in con.execute(f"SELECT {columns} FROM issues ORDER BY id"):
            digest.update(repr(row).encode("utf-8"))
        return digest.hexdigest()
    finally:
        con.close()


def _issue_count(path: Path) -> int:
    con = _connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
    finally:
        con.close()


def _issue_columns(path: Path) -> set[str]:
    con = _connect(path)
    try:
        return {row[1] for row in con.execute("PRAGMA table_info(issues)")}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# A. Migration behaviour
# ---------------------------------------------------------------------------
class TestAlembicMigration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "migration.db"
        self.url = sqlite_url(self.db_path)

    def test_empty_database_is_initialised_to_head(self):
        result = prepare_database(self.url)

        self.assertEqual(result.state, "initialized")
        self.assertEqual(result.revision, HEAD_REVISION)
        self.assertEqual(current_revision(self.url), HEAD_REVISION)

        columns = _issue_columns(self.db_path)
        self.assertTrue(set(EXPLAINABILITY_COLUMNS).issubset(columns))
        con = _connect(self.db_path)
        try:
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            con.close()
        self.assertTrue(
            {"users", "departments", "issues", "issue_images",
             "issue_status_updates", "alembic_version"}.issubset(tables)
        )

    def test_pre_task6_schema_is_stamped_without_recreating_tables(self):
        _create_baseline_database(self.url)
        _seed_historical_row(self.db_path)
        before_digest = _issue_digest(self.db_path)
        before_count = _issue_count(self.db_path)

        result = prepare_database(self.url)

        self.assertEqual(result.state, "stamped_baseline")
        self.assertEqual(result.revision, HEAD_REVISION)
        self.assertEqual(current_revision(self.url), HEAD_REVISION)
        self.assertEqual(_issue_count(self.db_path), before_count)
        self.assertEqual(_issue_digest(self.db_path), before_digest)
        self.assertTrue(set(EXPLAINABILITY_COLUMNS).issubset(_issue_columns(self.db_path)))

        con = _connect(self.db_path)
        try:
            row = con.execute(
                "SELECT id, category_model_version, category_needs_review, "
                "category_review_reason, category_top_alternatives, "
                "category_score_kind, priority_reasons, priority_score, "
                "priority_level, resolved_at FROM issues WHERE id = 'hist-issue-1'"
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(row[0], "hist-issue-1")
        self.assertEqual(list(row[1:]), [None] * 9)

    def test_repeated_prepare_is_safe(self):
        _create_baseline_database(self.url)
        _seed_historical_row(self.db_path)

        first = prepare_database(self.url)
        digest = _issue_digest(self.db_path)
        second = prepare_database(self.url)

        self.assertEqual(first.revision, HEAD_REVISION)
        self.assertEqual(second.state, "already_current")
        self.assertEqual(second.revision, HEAD_REVISION)
        self.assertEqual(_issue_digest(self.db_path), digest)
        self.assertEqual(_issue_count(self.db_path), 1)

    def test_downgrade_on_disposable_database_removes_only_new_columns(self):
        _create_baseline_database(self.url)
        _seed_historical_row(self.db_path)
        prepare_database(self.url)
        digest = _issue_digest(self.db_path)

        command.downgrade(make_alembic_config(self.url), BASELINE_REVISION)

        self.assertEqual(current_revision(self.url), BASELINE_REVISION)
        self.assertFalse(set(EXPLAINABILITY_COLUMNS) & _issue_columns(self.db_path))
        self.assertEqual(_issue_count(self.db_path), 1)
        self.assertEqual(_issue_digest(self.db_path), digest)

    def test_partially_applied_schema_is_refused(self):
        _create_baseline_database(self.url)
        con = _connect(self.db_path)
        try:
            con.execute(
                "ALTER TABLE issues ADD COLUMN category_model_version VARCHAR(80)"
            )
            con.commit()
        finally:
            con.close()

        with self.assertRaises(MigrationError) as ctx:
            prepare_database(self.url)
        self.assertIn("partially applied", str(ctx.exception).lower())
        self.assertIsNone(current_revision(self.url))

    def test_incompatible_database_is_not_stamped(self):
        con = _connect(self.db_path)
        try:
            con.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
            con.commit()
        finally:
            con.close()

        with self.assertRaises(DatabaseSchemaError):
            prepare_database(self.url)

        con = _connect(self.db_path)
        try:
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            con.close()
        self.assertNotIn("alembic_version", tables)


# ---------------------------------------------------------------------------
# B/C/E. API-level persistence, priority and exposure
# ---------------------------------------------------------------------------
class _ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.password_hash = hash_password("Task6Explain#2026")

    def setUp(self):
        self.db = TestingSessionLocal()
        self.old_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_db] = override_get_db
        self._clear()
        self._seed()

    def tearDown(self):
        self.db.close()
        app.dependency_overrides = self.old_overrides

    def _clear(self):
        self.db.query(models.IssueStatusUpdate).delete()
        self.db.query(models.IssueImage).delete()
        self.db.query(models.Issue).delete()
        self.db.query(models.User).delete()
        self.db.query(models.Department).delete()
        self.db.commit()

    def _seed(self):
        self.dept_roads = models.Department(
            name="TEST_TASK6_ROADS",
            handles_category=models.IssueCategory.ROAD_DAMAGE,
            city=CITY_BENGALURU,
            source_agency="TEST_TASK6",
        )
        self.dept_garbage = models.Department(
            name="TEST_TASK6_GARBAGE",
            handles_category=models.IssueCategory.GARBAGE_WASTE,
            city=CITY_GURUGRAM,
            source_agency="TEST_TASK6",
        )
        self.db.add_all([self.dept_roads, self.dept_garbage])
        self.db.commit()

        self.citizen = models.User(
            name="TEST_TASK6_CITIZEN",
            email="task6.citizen@uimrs.test",
            password_hash=self.password_hash,
            role=models.UserRole.CITIZEN,
        )
        self.other_citizen = models.User(
            name="TEST_TASK6_OTHER",
            email="task6.other@uimrs.test",
            password_hash=self.password_hash,
            role=models.UserRole.CITIZEN,
        )
        self.admin = models.User(
            name="TEST_TASK6_ADMIN",
            email="task6.admin@uimrs.test",
            password_hash=self.password_hash,
            role=models.UserRole.ADMIN,
        )
        self.db.add_all([self.citizen, self.other_citizen, self.admin])
        self.db.commit()

        self.citizen_token = create_access_token(self.citizen)
        self.other_citizen_token = create_access_token(self.other_citizen)
        self.admin_token = create_access_token(self.admin)

    def _issue_from_response(self, response) -> models.Issue:
        self.assertEqual(response.status_code, 201, response.text)
        return (
            self.db.query(models.Issue)
            .filter(models.Issue.id == response.json()["id"])
            .first()
        )

    @staticmethod
    def _bengaluru_payload() -> dict:
        return {
            "title": "Large pothole on the main road",
            "description": "A deep pothole is causing accidents near the junction.",
            "latitude": 12.9716,
            "longitude": 77.5946,
            "city": "Bangalore, Karnataka, India",
        }

    @staticmethod
    def _gurugram_payload() -> dict:
        return {
            "title": "Garbage dumped near the market",
            "description": "Garbage is dumped near the market and not collected for days.",
            "latitude": 28.4601,
            "longitude": 77.0648,
            "city": "Gurgaon, Haryana, India",
        }

    @staticmethod
    def _ambiguous_gurugram_payload() -> dict:
        return {
            "title": "Problem near the park area",
            "description": "This situation is unacceptable and needs attention soon.",
            "latitude": 28.4601,
            "longitude": 77.0648,
            "city": "Gurugram, Haryana, India",
        }


class TestClassificationPersistence(_ApiTestCase):
    def test_bengaluru_model_version_score_kind_and_alternatives_are_persisted(self):
        alternatives = [
            {"category": "Road_Damage", "model_score": 0.9},
            {"category": "Drainage_Sewage", "model_score": 0.05},
        ]
        with patch("app.routers.issues.classifier.classify") as classify_mock:
            classify_mock.return_value = ClassificationResult(
                models.IssueCategory.ROAD_DAMAGE,
                0.9,
                "bengaluru_v2_2_ml",
                needs_review=False,
                review_reason=None,
                model_version=BENGALURU_MODEL_VERSION,
                top_alternatives=alternatives,
            )
            response = self.client.post(
                "/api/v1/issues",
                json=self._bengaluru_payload(),
                headers=bearer(self.citizen_token),
            )

        issue = self._issue_from_response(response)
        self.assertEqual(issue.category_model_version, BENGALURU_MODEL_VERSION)
        self.assertEqual(issue.category_score_kind, "uncalibrated_model_score")
        self.assertIs(issue.category_needs_review, False)
        self.assertIsNone(issue.category_review_reason)
        self.assertEqual(issue.category_top_alternatives, alternatives)

    def test_low_score_clarification_persists_review_flag_and_reason(self):
        alternatives = [
            {"category": "Road_Damage", "model_score": 0.42},
            {"category": "Drainage_Sewage", "model_score": 0.38},
        ]
        reason = "Low model score or small top-two margin"
        with patch("app.routers.issues.classifier.classify") as classify_mock:
            classify_mock.return_value = ClassificationResult(
                models.IssueCategory.OTHER,
                0.0,
                "needs_clarification",
                needs_review=True,
                review_reason=reason,
                model_version=BENGALURU_MODEL_VERSION,
                top_alternatives=alternatives,
            )
            response = self.client.post(
                "/api/v1/issues",
                json=self._bengaluru_payload(),
                headers=bearer(self.citizen_token),
            )

        issue = self._issue_from_response(response)
        self.assertEqual(issue.category, models.IssueCategory.OTHER)
        self.assertEqual(issue.category_source, "needs_clarification")
        self.assertEqual(issue.category_score_kind, "none")
        self.assertIs(issue.category_needs_review, True)
        self.assertEqual(issue.category_review_reason, reason)
        self.assertEqual(issue.category_model_version, BENGALURU_MODEL_VERSION)
        self.assertEqual(issue.category_top_alternatives, alternatives)

    def test_gurugram_rule_based_classification_never_gets_bengaluru_model_version(self):
        with patch.object(
            IssueClassifier,
            "_bengaluru_scores",
            side_effect=AssertionError("Bengaluru model must not run for Gurugram"),
        ):
            response = self.client.post(
                "/api/v1/issues",
                json=self._gurugram_payload(),
                headers=bearer(self.citizen_token),
            )

        issue = self._issue_from_response(response)
        self.assertEqual(issue.category, models.IssueCategory.GARBAGE_WASTE)
        self.assertEqual(issue.category_source, "india_keyword_rule")
        self.assertEqual(issue.category_score_kind, "rule_heuristic")
        self.assertIsNone(issue.category_model_version)
        self.assertIsNone(issue.category_top_alternatives)
        self.assertIs(issue.category_needs_review, False)
        self.assertIsNone(issue.category_review_reason)

    def test_gurugram_clarification_persists_actual_review_reason(self):
        response = self.client.post(
            "/api/v1/issues",
            json=self._ambiguous_gurugram_payload(),
            headers=bearer(self.citizen_token),
        )

        issue = self._issue_from_response(response)
        self.assertEqual(issue.category, models.IssueCategory.OTHER)
        self.assertEqual(issue.category_source, "needs_clarification")
        self.assertEqual(issue.category_score_kind, "none")
        self.assertIs(issue.category_needs_review, True)
        self.assertEqual(
            issue.category_review_reason, "No clear Indian keyword rule matched"
        )
        self.assertIsNone(issue.category_model_version)
        self.assertIsNone(issue.category_top_alternatives)

    def test_score_kind_mapping_distinguishes_ml_rules_and_clarification(self):
        self.assertEqual(
            score_kind_for_source("bengaluru_v2_2_ml"), "uncalibrated_model_score"
        )
        self.assertEqual(score_kind_for_source("india_keyword_rule"), "rule_heuristic")
        self.assertEqual(score_kind_for_source("needs_clarification"), "none")
        self.assertEqual(score_kind_for_source("empty_input"), "none")
        self.assertIsNone(score_kind_for_source("unrecognised_source"))
        self.assertIsNone(score_kind_for_source(None))


class TestPriorityPersistence(_ApiTestCase):
    def test_real_priority_reasons_are_persisted_verbatim(self):
        payload = self._gurugram_payload()
        expected = PriorityEngine().score(
            payload["description"], "Garbage_Waste", duplicate_count=0
        )

        response = self.client.post(
            "/api/v1/issues", json=payload, headers=bearer(self.citizen_token)
        )
        issue = self._issue_from_response(response)

        self.assertTrue(issue.priority_reasons)
        self.assertEqual(issue.priority_reasons, expected.reasons)
        self.assertAlmostEqual(issue.priority_score, expected.score)
        self.assertEqual(issue.priority_level.value, expected.level)
        self.assertTrue(issue.priority_reasons[0].startswith("Base severity for Garbage_Waste:"))

    def test_no_reasons_are_invented_when_the_engine_returns_none(self):
        with patch("app.routers.issues.priority_engine.score") as score_mock:
            score_mock.return_value = PriorityResult(5.0, "Medium", [])
            response = self.client.post(
                "/api/v1/issues",
                json=self._gurugram_payload(),
                headers=bearer(self.citizen_token),
            )

        issue = self._issue_from_response(response)
        self.assertIsNone(issue.priority_reasons)
        self.assertEqual(issue.priority_score, 5.0)
        self.assertEqual(issue.priority_level.value, "Medium")

    def test_priority_formula_is_unchanged_and_deterministic(self):
        engine = PriorityEngine()
        text = "Garbage is dumped near the market and not collected for days."
        first = engine.score(text, "Garbage_Waste", duplicate_count=0)
        second = engine.score(text, "Garbage_Waste", duplicate_count=0)

        self.assertEqual(first.score, second.score)
        self.assertEqual(first.reasons, second.reasons)
        self.assertIn(first.level, {"Low", "Medium", "High", "Critical"})
        self.assertTrue(first.reasons)


class TestApiExposure(_ApiTestCase):
    def _create_explained_live_issue(self):
        alternatives = [{"category": "Road_Damage", "model_score": 0.88}]
        with patch("app.routers.issues.classifier.classify") as classify_mock:
            classify_mock.return_value = ClassificationResult(
                models.IssueCategory.ROAD_DAMAGE,
                0.88,
                "bengaluru_v2_2_ml",
                needs_review=False,
                model_version=BENGALURU_MODEL_VERSION,
                top_alternatives=alternatives,
            )
            response = self.client.post(
                "/api/v1/issues",
                json=self._bengaluru_payload(),
                headers=bearer(self.citizen_token),
            )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"], alternatives

    def test_authenticated_detail_exposes_explanations(self):
        issue_id, alternatives = self._create_explained_live_issue()

        response = self.client.get(
            f"/api/v1/issues/{issue_id}", headers=bearer(self.citizen_token)
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["category_model_version"], BENGALURU_MODEL_VERSION)
        self.assertEqual(body["category_score_kind"], "uncalibrated_model_score")
        self.assertIs(body["category_needs_review"], False)
        self.assertEqual(body["category_top_alternatives"], alternatives)
        self.assertTrue(body["priority_reasons"])

    def test_authorised_list_exposes_explanations(self):
        issue_id, alternatives = self._create_explained_live_issue()

        response = self.client.get(
            "/api/v1/issues", headers=bearer(self.citizen_token)
        )

        self.assertEqual(response.status_code, 200, response.text)
        item = next(entry for entry in response.json() if entry["id"] == issue_id)
        self.assertEqual(item["category_model_version"], BENGALURU_MODEL_VERSION)
        self.assertEqual(item["category_score_kind"], "uncalibrated_model_score")
        self.assertEqual(item["category_top_alternatives"], alternatives)
        self.assertTrue(item["priority_reasons"])

    def test_public_dashboard_does_not_expose_explanations_or_reporter(self):
        historical = models.Issue(
            ticket_number="TEST-TASK6-HIST-DASH",
            title="Historical dashboard fixture",
            description="Isolated fixture, not a real complaint.",
            category=models.IssueCategory.ROAD_DAMAGE,
            status=models.IssueStatus.REPORTED,
            latitude=12.9716,
            longitude=77.5946,
            reporter_id=self.citizen.id,
            city=CITY_BENGALURU,
            is_historical=True,
            source_record_id="TEST-TASK6-DASH-1",
            # Deliberately populated to prove the public contract never leaks them.
            category_model_version=BENGALURU_MODEL_VERSION,
            category_needs_review=True,
            category_review_reason="should not leak",
            category_top_alternatives=[{"category": "Road_Damage", "model_score": 0.9}],
            category_score_kind="uncalibrated_model_score",
            priority_reasons=["should not leak"],
        )
        self.db.add(historical)
        self.db.commit()

        response = self.client.get("/api/v1/issues/dashboard")

        self.assertEqual(response.status_code, 200, response.text)
        item = next(entry for entry in response.json() if entry["id"] == historical.id)
        for field in EXPLAINABILITY_COLUMNS:
            self.assertNotIn(field, item)
        self.assertNotIn("reporter_id", item)
        self.assertNotIn("description", item)
        self.assertNotIn("category_confidence", item)

    def test_existing_read_access_rules_are_unchanged(self):
        issue_id, _ = self._create_explained_live_issue()

        other = self.client.get(
            f"/api/v1/issues/{issue_id}", headers=bearer(self.other_citizen_token)
        )
        self.assertEqual(other.status_code, 404)

        admin = self.client.get(
            f"/api/v1/issues/{issue_id}", headers=bearer(self.admin_token)
        )
        self.assertEqual(admin.status_code, 200)
        self.assertEqual(admin.json()["id"], issue_id)


# ---------------------------------------------------------------------------
# D. Historical-data preservation through the real migration
# ---------------------------------------------------------------------------
class TestHistoricalDataProtection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "historical.db"
        self.url = sqlite_url(self.db_path)
        _create_baseline_database(self.url)
        self.provenance = _seed_historical_row(self.db_path)

    def _historical_row(self):
        con = _connect(self.db_path)
        try:
            return con.execute(
                "SELECT category_model_version, category_needs_review, "
                "category_review_reason, category_top_alternatives, "
                "category_score_kind, priority_reasons, priority_score, "
                "priority_level, resolved_at, city, source_dataset, "
                "source_record_id, source_status, source_agency, "
                "source_category, source_subcategory "
                "FROM issues WHERE id = 'hist-issue-1'"
            ).fetchone()
        finally:
            con.close()

    def test_historical_row_keeps_explanations_and_priority_null(self):
        before_digest = _issue_digest(self.db_path)
        prepare_database(self.url)

        row = self._historical_row()
        self.assertEqual(list(row[:9]), [None] * 9)
        self.assertEqual(_issue_digest(self.db_path), before_digest)
        self.assertEqual(_issue_count(self.db_path), 1)

    def test_historical_provenance_is_byte_for_byte_preserved(self):
        prepare_database(self.url)

        row = self._historical_row()
        self.assertEqual(row[9], self.provenance["city"])
        self.assertEqual(row[10], self.provenance["source_dataset"])
        self.assertEqual(row[11], self.provenance["source_record_id"])
        self.assertEqual(row[12], self.provenance["source_status"])
        self.assertEqual(row[13], self.provenance["source_agency"])
        self.assertEqual(row[14], self.provenance["source_category"])
        self.assertEqual(row[15], self.provenance["source_subcategory"])

    def test_repeated_migration_does_not_touch_historical_rows(self):
        prepare_database(self.url)
        first = self._historical_row()
        first_digest = _issue_digest(self.db_path)

        prepare_database(self.url)

        self.assertEqual(self._historical_row(), first)
        self.assertEqual(_issue_digest(self.db_path), first_digest)


# ---------------------------------------------------------------------------
# F. Schema guard
# ---------------------------------------------------------------------------
class TestSchemaGuardExplainability(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "guard.db"
        self.url = sqlite_url(self.db_path)

    def _guard(self):
        engine = create_engine(self.url)
        self.addCleanup(engine.dispose)
        return guard_startup(engine, self.url)

    def test_guard_accepts_migrated_schema(self):
        prepare_database(self.url)
        self.assertEqual(self._guard(), "validated")
        engine = create_engine(self.url)
        self.addCleanup(engine.dispose)
        validate_schema(engine)  # must not raise

    def test_guard_rejects_database_missing_new_columns(self):
        _create_baseline_database(self.url, drop_alembic_version=False)

        with self.assertRaises(DatabaseSchemaError) as ctx:
            self._guard()

        message = str(ctx.exception)
        for column in EXPLAINABILITY_COLUMNS:
            self.assertIn(column, message)
        self.assertIn("No schema was changed automatically", message)

    def test_guard_rejects_partially_migrated_database(self):
        _create_baseline_database(self.url, drop_alembic_version=False)
        con = _connect(self.db_path)
        try:
            con.execute(
                "ALTER TABLE issues ADD COLUMN category_model_version VARCHAR(80)"
            )
            con.commit()
        finally:
            con.close()

        with self.assertRaises(DatabaseSchemaError):
            self._guard()


if __name__ == "__main__":
    unittest.main(verbosity=2)
