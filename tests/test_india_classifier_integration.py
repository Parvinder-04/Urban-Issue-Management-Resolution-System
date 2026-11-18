"""
UIMRS Task 5 — City-aware Indian classifier integration tests.

Verifies:
A. Bengaluru uses the verified V2.2 candidate, with the training-time input
   format, explicit label mapping and conservative abstention guards.
B. Gurugram never invokes the Bengaluru or NYC models and uses deterministic
   Indian keyword rules only, labelled as rule-based.
C. Unknown cities never invoke either model and fall back to rules or review.
D. Priority scoring, duplicate detection, department lookup, citizen submission
   and historical immutability still work.
E. The real candidate artifact loads and classifies isolated representative
   text without touching any project database or raw training dataset.

Every API test uses an isolated in-memory SQLite database. No real project
database is opened or modified.
"""
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import joblib
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.main import app
from app.routers.auth import create_access_token, hash_password
from app.services import classification_service
from app.services.classification_service import (
    BENGALURU_MODEL_PATH,
    BENGALURU_MODEL_VERSION,
    CITY_BENGALURU,
    CITY_GURUGRAM,
    ClassificationResult,
    IssueClassifier,
    MODEL_LABEL_TO_CATEGORY,
    PriorityEngine,
    canonical_city,
    combine_complaint_text,
)
from app.services.geo_clustering_service import GeoPoint, find_duplicates

V2_2_LABELS = [
    "Road_Damage",
    "Garbage_Waste",
    "Streetlight",
    "Water_Supply",
    "Drainage_Sewage",
    "Traffic_Management",
    "Trees_Environment",
]

# The public release intentionally does not redistribute the Bengaluru model
# artifact. Tests that require the real artifact are skipped with a clear
# reason; no test is deleted and no substitute model is used.
MODEL_ARTIFACT_AVAILABLE = BENGALURU_MODEL_PATH.is_file()
MODEL_ARTIFACT_SKIP_REASON = (
    "Bengaluru classifier artifact is not redistributed in this public release"
)


class _FakeEstimator:
    def __init__(self, classes):
        self.classes_ = np.array(classes)


class _FakePipeline:
    """Minimal stand-in exposing the same surface as the sklearn Pipeline."""

    def __init__(self, classes, probabilities):
        self.named_steps = {"classifier": _FakeEstimator(classes)}
        self._probabilities = np.asarray(probabilities, dtype=float)
        self.seen_texts = []

    def predict_proba(self, texts):
        self.seen_texts.extend(list(texts))
        return np.array([self._probabilities])


def fake_bengaluru_classifier(classes, probabilities):
    classifier = IssueClassifier()
    classifier._bengaluru_model = _FakePipeline(classes, probabilities)
    classifier._bengaluru_load_attempted = True
    classifier._bengaluru_load_error = None
    return classifier


class TestCityNormalization(unittest.TestCase):
    def test_documented_aliases_map_to_canonical_cities(self):
        cases = {
            "Bangalore": CITY_BENGALURU,
            "bangalore, karnataka, india": CITY_BENGALURU,
            "Bengaluru": CITY_BENGALURU,
            "Bengaluru, Karnataka, India": CITY_BENGALURU,
            "Gurgaon": CITY_GURUGRAM,
            "gurgaon, haryana, india": CITY_GURUGRAM,
            "Gurugram": CITY_GURUGRAM,
            "Gurugram, Haryana, India": CITY_GURUGRAM,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(canonical_city(value), expected)

    def test_unknown_city_is_not_silently_treated_as_bengaluru(self):
        for value in ("Pune", "Mumbai, Maharashtra, India", "", None, "Unknown"):
            with self.subTest(value=value):
                self.assertIsNone(canonical_city(value))

    def test_training_input_combines_title_and_description(self):
        self.assertEqual(
            combine_complaint_text("  Large pothole  ", " on the main road "),
            "Large pothole on the main road",
        )
        self.assertEqual(combine_complaint_text("", "only description"), "only description")
        self.assertEqual(combine_complaint_text("only title", ""), "only title")


class TestBengaluruClassifier(unittest.TestCase):
    @unittest.skipUnless(MODEL_ARTIFACT_AVAILABLE, MODEL_ARTIFACT_SKIP_REASON)
    def test_v2_2_artifact_path_is_project_relative_and_present(self):
        self.assertTrue(BENGALURU_MODEL_PATH.is_file())
        self.assertEqual(
            BENGALURU_MODEL_PATH,
            PROJECT_ROOT
            / "backend"
            / "app"
            / "ml_models"
            / "india_bengaluru_v2_2_candidate"
            / "classifier.joblib",
        )

    @unittest.skipUnless(MODEL_ARTIFACT_AVAILABLE, MODEL_ARTIFACT_SKIP_REASON)
    def test_real_v2_2_model_loads_with_expected_pipeline(self):
        classifier = IssueClassifier()
        model = classifier._load_bengaluru_model()
        self.assertIsNotNone(model)
        self.assertEqual(set(model.named_steps), {"tfidf", "classifier"})

        classes = list(classifier._model_classes(model))
        self.assertEqual(sorted(classes), sorted(V2_2_LABELS))

        vectorizer = model.named_steps["tfidf"]
        self.assertTrue(vectorizer.lowercase)
        self.assertEqual(vectorizer.ngram_range, (1, 2))
        self.assertEqual(vectorizer.min_df, 2)
        self.assertEqual(vectorizer.max_features, 100_000)
        self.assertTrue(vectorizer.sublinear_tf)
        self.assertEqual(vectorizer.strip_accents, "unicode")

    @unittest.skipUnless(MODEL_ARTIFACT_AVAILABLE, MODEL_ARTIFACT_SKIP_REASON)
    def test_metadata_is_bengaluru_experimental_only(self):
        metadata = IssueClassifier().metadata
        self.assertEqual(metadata["geography"], CITY_BENGALURU)
        self.assertEqual(metadata["candidate_status"], "experimental_candidate_not_production")
        self.assertAlmostEqual(metadata["cross_validation"]["mean_accuracy"], 0.8874)
        self.assertAlmostEqual(metadata["cross_validation"]["mean_macro_f1"], 0.8345)
        self.assertTrue(
            any("not Gurugram" in limitation for limitation in metadata["limitations"])
        )

    def test_label_mapping_covers_only_verified_v2_2_labels(self):
        self.assertEqual(set(MODEL_LABEL_TO_CATEGORY), set(V2_2_LABELS))
        for label, category in MODEL_LABEL_TO_CATEGORY.items():
            with self.subTest(label=label):
                self.assertEqual(category.value, label)
        self.assertNotIn("Traffic_Signal", MODEL_LABEL_TO_CATEGORY)

    def test_training_input_is_passed_to_the_model_unchanged(self):
        probabilities = [0.90, 0.03, 0.02, 0.02, 0.01, 0.01, 0.01]
        classifier = fake_bengaluru_classifier(V2_2_LABELS, probabilities)

        result = classifier.classify("  Large pothole  ", CITY_BENGALURU)

        self.assertEqual(classifier._bengaluru_model.seen_texts, ["Large pothole"])
        self.assertEqual(result.category, models.IssueCategory.ROAD_DAMAGE)
        self.assertEqual(result.source, "bengaluru_v2_2_ml")

    def test_high_scoring_supported_prediction_is_accepted(self):
        probabilities = [0.90, 0.03, 0.02, 0.02, 0.01, 0.01, 0.01]
        classifier = fake_bengaluru_classifier(V2_2_LABELS, probabilities)

        result = classifier.classify("Large pothole on the main road", CITY_BENGALURU)

        self.assertEqual(result.category, models.IssueCategory.ROAD_DAMAGE)
        self.assertEqual(result.source, "bengaluru_v2_2_ml")
        self.assertFalse(result.needs_review)
        self.assertIsNone(result.review_reason)
        self.assertAlmostEqual(result.confidence, 0.90)
        self.assertEqual(result.model_version, BENGALURU_MODEL_VERSION)
        self.assertTrue(result.top_alternatives)

    def test_low_score_triggers_clarification(self):
        # Top score 0.40 is below the 0.55 guard.
        probabilities = [0.40, 0.30, 0.10, 0.05, 0.05, 0.05, 0.05]
        classifier = fake_bengaluru_classifier(V2_2_LABELS, probabilities)

        result = classifier.classify("Something is wrong here", CITY_BENGALURU)

        self.assertEqual(result.category, models.IssueCategory.OTHER)
        self.assertEqual(result.source, "needs_clarification")
        self.assertTrue(result.needs_review)
        self.assertIn("score", result.review_reason.lower())

    def test_low_top_two_margin_triggers_clarification(self):
        # Top score 0.55 passes the score guard; margin 0.11 fails the 0.15 guard.
        probabilities = [0.55, 0.44, 0.01, 0.00, 0.00, 0.00, 0.00]
        classifier = fake_bengaluru_classifier(V2_2_LABELS, probabilities)

        result = classifier.classify("Unclear civic complaint", CITY_BENGALURU)

        self.assertEqual(result.category, models.IssueCategory.OTHER)
        self.assertEqual(result.source, "needs_clarification")
        self.assertTrue(result.needs_review)
        self.assertIn("margin", result.review_reason.lower())

    def test_unknown_model_label_is_never_silently_accepted(self):
        classes = ["Alien_Label"] + V2_2_LABELS[1:]
        probabilities = [0.90, 0.03, 0.02, 0.02, 0.01, 0.01, 0.01]
        classifier = fake_bengaluru_classifier(classes, probabilities)

        result = classifier.classify("Large pothole on the main road", CITY_BENGALURU)

        self.assertEqual(result.category, models.IssueCategory.OTHER)
        self.assertEqual(result.source, "needs_clarification")
        self.assertTrue(result.needs_review)
        self.assertIn("Unsupported model label", result.review_reason)

    def test_missing_artifact_returns_safe_review_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            classifier = IssueClassifier(
                model_path=Path(tmp) / "missing.joblib",
                metadata_path=Path(tmp) / "missing.json",
            )
            result = classifier.classify("Large pothole on the main road", CITY_BENGALURU)

        self.assertEqual(result.category, models.IssueCategory.OTHER)
        self.assertEqual(result.source, "needs_clarification")
        self.assertTrue(result.needs_review)
        self.assertIn("unavailable", result.review_reason.lower())

    def test_corrupt_artifact_returns_safe_review_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            corrupt = Path(tmp) / "classifier.joblib"
            corrupt.write_bytes(b"this is not a joblib artifact")
            classifier = IssueClassifier(
                model_path=corrupt,
                metadata_path=Path(tmp) / "missing.json",
            )
            result = classifier.classify("Large pothole on the main road", CITY_BENGALURU)

        self.assertEqual(result.category, models.IssueCategory.OTHER)
        self.assertEqual(result.source, "needs_clarification")
        self.assertTrue(result.needs_review)

    @unittest.skipUnless(MODEL_ARTIFACT_AVAILABLE, MODEL_ARTIFACT_SKIP_REASON)
    def test_model_is_loaded_once_not_per_complaint(self):
        classifier = IssueClassifier()
        with patch(
            "app.services.classification_service.joblib.load", wraps=joblib.load
        ) as load_mock:
            classifier.classify("Large pothole on the main road", CITY_BENGALURU)
            classifier.classify("Garbage is dumped near the market", CITY_BENGALURU)

        self.assertEqual(load_mock.call_count, 1)


class TestGurugramClassifier(unittest.TestCase):
    def test_bengaluru_and_nyc_models_are_never_invoked(self):
        classifier = IssueClassifier()
        with patch.object(
            IssueClassifier,
            "_bengaluru_scores",
            side_effect=AssertionError("Bengaluru model must not be called for Gurugram"),
        ), patch(
            "app.services.classification_service.joblib.load",
            side_effect=AssertionError("No model may be loaded for Gurugram"),
        ):
            result = classifier.classify(
                "Garbage is dumped near the market and not collected",
                CITY_GURUGRAM,
            )

        self.assertEqual(result.category, models.IssueCategory.GARBAGE_WASTE)
        self.assertEqual(result.source, "india_keyword_rule")
        self.assertIsNone(result.model_version)
        self.assertNotEqual(result.source, "bengaluru_v2_2_ml")

    def test_clear_keyword_rule_uses_rule_based_source(self):
        classifier = IssueClassifier()
        result = classifier.classify(
            "Street light not working near the school", CITY_GURUGRAM
        )

        self.assertEqual(result.category, models.IssueCategory.STREETLIGHT)
        self.assertEqual(result.source, "india_keyword_rule")
        self.assertFalse(result.needs_review)
        self.assertIsNone(result.model_version)

    def test_ambiguous_complaint_triggers_clarification(self):
        classifier = IssueClassifier()
        result = classifier.classify(
            "There is a problem near the park please fix it", CITY_GURUGRAM
        )

        self.assertEqual(result.category, models.IssueCategory.OTHER)
        self.assertEqual(result.source, "needs_clarification")
        self.assertTrue(result.needs_review)

    def test_gurugram_prediction_is_never_labelled_bengaluru_ml(self):
        classifier = IssueClassifier()
        for text in (
            "Garbage is dumped near the market",
            "Water supply is not available in the colony",
            "There is a problem near the park",
        ):
            with self.subTest(text=text):
                result = classifier.classify(text, CITY_GURUGRAM)
                self.assertNotEqual(result.source, "bengaluru_v2_2_ml")
                self.assertIsNone(result.model_version)


class TestUnknownCityClassifier(unittest.TestCase):
    def test_unknown_city_never_uses_bengaluru_or_nyc_models(self):
        classifier = IssueClassifier()
        with patch.object(
            IssueClassifier,
            "_bengaluru_scores",
            side_effect=AssertionError("Bengaluru model must not be called"),
        ), patch(
            "app.services.classification_service.joblib.load",
            side_effect=AssertionError("No model may be loaded"),
        ):
            result = classifier.classify(
                "Garbage is dumped near the market", "Pune, Maharashtra, India"
            )

        self.assertEqual(result.category, models.IssueCategory.GARBAGE_WASTE)
        self.assertEqual(result.source, "india_keyword_rule")
        self.assertIsNone(result.model_version)

    def test_unknown_city_ambiguous_complaint_triggers_clarification(self):
        classifier = IssueClassifier()
        result = classifier.classify(
            "There is a problem near the park", "Pune, Maharashtra, India"
        )

        self.assertEqual(result.category, models.IssueCategory.OTHER)
        self.assertEqual(result.source, "needs_clarification")
        self.assertTrue(result.needs_review)

    def test_missing_city_is_treated_as_unknown_not_bengaluru(self):
        classifier = IssueClassifier()
        with patch.object(
            IssueClassifier,
            "_bengaluru_scores",
            side_effect=AssertionError("Bengaluru model must not be called"),
        ):
            result = classifier.classify("Garbage is dumped near the market", None)

        self.assertEqual(result.source, "india_keyword_rule")


class TestNYCDecommissioning(unittest.TestCase):
    def test_operational_service_no_longer_references_nyc_artifacts(self):
        source = inspect.getsource(classification_service)
        self.assertNotIn("classifier_uimrs_real", source)
        self.assertNotIn("vectorizer_uimrs_real", source)
        self.assertNotIn("real_classifier_metadata", source)

    def test_construction_does_not_load_any_model(self):
        with patch(
            "app.services.classification_service.joblib.load",
            side_effect=AssertionError("Construction must not load a model"),
        ):
            classifier = IssueClassifier()

        self.assertIsNone(classifier._bengaluru_model)
        self.assertFalse(classifier._bengaluru_load_attempted)
        self.assertEqual(Path(classifier.model_path), BENGALURU_MODEL_PATH)


class TestClassifierRegressions(unittest.TestCase):
    def test_priority_engine_still_scores(self):
        engine = PriorityEngine()
        baseline = engine.score("pothole on the road", "Road_Damage")
        urgent = engine.score(
            "dangerous pothole near a school, children are falling", "Road_Damage"
        )

        self.assertGreater(urgent.score, baseline.score)
        self.assertIn(urgent.level, {"Low", "Medium", "High", "Critical"})
        self.assertTrue(urgent.reasons)

    def test_duplicate_detection_service_still_works(self):
        original = GeoPoint("original", 28.4601, 77.0648, "Garbage_Waste")
        nearby = GeoPoint("nearby", 28.46015, 77.06485, "Garbage_Waste")
        far_away = GeoPoint("far", 28.5000, 77.1000, "Garbage_Waste")

        self.assertEqual(find_duplicates(nearby, [original], radius_meters=50), ["original"])
        self.assertEqual(find_duplicates(far_away, [original], radius_meters=50), [])


# ---------------------------------------------------------------------------
# Route-level regression tests (isolated in-memory SQLite)
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


class TestIssueSubmissionRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.password_hash = hash_password("Task5Regression#2026")

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

        self.department = models.Department(
            name="TEST_TASK5_GARBAGE",
            handles_category=models.IssueCategory.GARBAGE_WASTE,
            city=CITY_GURUGRAM,
            source_agency="TEST_TASK5_AGENCY",
            # Task 8: an automatic assignment requires explicit verification
            # provenance. This isolated fixture stands in for a verified MCG
            # sanitation mapping so the pre-existing routing assertion stays
            # meaningful instead of being weakened.
            routing_mode="automatic",
            verification_source_url="https://mcg.gov.in/contact/office-contacts",
            verification_date="2026-11-14",
        )
        self.db.add(self.department)
        self.db.commit()

        self.citizen = models.User(
            name="TEST_TASK5_CITIZEN",
            email="task5.citizen@uimrs.org",
            password_hash=self.password_hash,
            role=models.UserRole.CITIZEN,
        )
        self.admin = models.User(
            name="TEST_TASK5_ADMIN",
            email="task5.admin@uimrs.org",
            password_hash=self.password_hash,
            role=models.UserRole.ADMIN,
        )
        self.db.add_all([self.citizen, self.admin])
        self.db.commit()

        self.citizen_token = create_access_token(self.citizen)
        self.admin_token = create_access_token(self.admin)

    def tearDown(self):
        self.db.close()
        app.dependency_overrides = self.old_overrides

    def _gurugram_payload(self):
        return {
            "title": "Garbage dumped near the market",
            "description": "Garbage is dumped near the market and not collected for days.",
            "latitude": 28.4601,
            "longitude": 77.0648,
            "city": "Gurgaon, Haryana, India",
        }

    def test_router_combines_text_and_passes_canonical_city(self):
        with patch("app.routers.issues.classifier.classify") as classify_mock:
            classify_mock.return_value = ClassificationResult(
                models.IssueCategory.ROAD_DAMAGE,
                0.9,
                "bengaluru_v2_2_ml",
                model_version=BENGALURU_MODEL_VERSION,
            )
            response = self.client.post(
                "/api/v1/issues",
                json={
                    "title": "Large pothole on the main road",
                    "description": "A deep pothole is causing accidents.",
                    "latitude": 12.9716,
                    "longitude": 77.5946,
                    "city": "Bangalore, Karnataka, India",
                },
                headers=bearer(self.citizen_token),
            )

        self.assertEqual(response.status_code, 201, response.text)
        classify_mock.assert_called_once_with(
            "Large pothole on the main road A deep pothole is causing accidents.",
            CITY_BENGALURU,
        )

        issue = (
            self.db.query(models.Issue)
            .filter(models.Issue.id == response.json()["id"])
            .first()
        )
        self.assertEqual(issue.category, models.IssueCategory.ROAD_DAMAGE)
        self.assertEqual(issue.category_source, "bengaluru_v2_2_ml")
        self.assertAlmostEqual(issue.category_confidence, 0.9)
        self.assertEqual(issue.city, CITY_BENGALURU)

    def test_gurugram_submission_is_rule_based_and_department_routed(self):
        with patch.object(
            IssueClassifier,
            "_bengaluru_scores",
            side_effect=AssertionError("Bengaluru model must not be used"),
        ):
            response = self.client.post(
                "/api/v1/issues",
                json=self._gurugram_payload(),
                headers=bearer(self.citizen_token),
            )

        self.assertEqual(response.status_code, 201, response.text)
        issue = (
            self.db.query(models.Issue)
            .filter(models.Issue.id == response.json()["id"])
            .first()
        )
        self.assertEqual(issue.category, models.IssueCategory.GARBAGE_WASTE)
        self.assertEqual(issue.category_source, "india_keyword_rule")
        self.assertEqual(issue.department_id, self.department.id)
        self.assertEqual(issue.city, CITY_GURUGRAM)

    def test_duplicate_detection_still_links_nearby_open_issue(self):
        first = self.client.post(
            "/api/v1/issues",
            json=self._gurugram_payload(),
            headers=bearer(self.citizen_token),
        )
        self.assertEqual(first.status_code, 201, first.text)

        second = self.client.post(
            "/api/v1/issues",
            json=self._gurugram_payload(),
            headers=bearer(self.citizen_token),
        )
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(second.json()["status"], "duplicate")

        original = (
            self.db.query(models.Issue)
            .filter(models.Issue.id == first.json()["id"])
            .first()
        )
        self.assertEqual(original.duplicate_count, 1)

    def test_historical_immutability_still_enforced(self):
        historical = models.Issue(
            ticket_number="TEST-TASK5-HIST-1",
            title="Historical Bengaluru fixture",
            description="Isolated fixture, not a real complaint.",
            category=models.IssueCategory.GARBAGE_WASTE,
            status=models.IssueStatus.REPORTED,
            latitude=12.9716,
            longitude=77.5946,
            reporter_id=self.admin.id,
            department_id=self.department.id,
            city=CITY_BENGALURU,
            is_historical=True,
            source_record_id="TEST-HIST-1",
        )
        self.db.add(historical)
        self.db.commit()

        response = self.client.patch(
            f"/api/v1/issues/{historical.id}/status",
            json={"new_status": "in_progress"},
            headers=bearer(self.admin_token),
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("historical", response.json()["detail"].lower())
        self.db.refresh(historical)
        self.assertEqual(historical.status, models.IssueStatus.REPORTED)


@unittest.skipUnless(MODEL_ARTIFACT_AVAILABLE, MODEL_ARTIFACT_SKIP_REASON)
class TestRealCandidateIntegration(unittest.TestCase):
    """Integration with the actual V2.2 artifact using isolated text only."""

    def test_real_candidate_classifies_representative_text(self):
        classifier = IssueClassifier()
        result = classifier.classify(
            "Large pothole on the main road causing accidents", CITY_BENGALURU
        )

        self.assertEqual(result.category, models.IssueCategory.ROAD_DAMAGE)
        self.assertEqual(result.source, "bengaluru_v2_2_ml")
        self.assertEqual(result.model_version, BENGALURU_MODEL_VERSION)
        self.assertFalse(result.needs_review)
        self.assertGreaterEqual(result.confidence, 0.55)
        self.assertTrue(result.top_alternatives)

    def test_real_candidate_abstains_on_ambiguous_text(self):
        classifier = IssueClassifier()
        result = classifier.classify(
            "Something is wrong here please fix it", CITY_BENGALURU
        )

        self.assertEqual(result.category, models.IssueCategory.OTHER)
        self.assertEqual(result.source, "needs_clarification")
        self.assertTrue(result.needs_review)
        self.assertTrue(result.top_alternatives)

    def test_real_candidate_does_not_touch_any_database(self):
        # This test intentionally uses no SQLAlchemy session, engine or fixture.
        classifier = IssueClassifier()
        result = classifier.classify(
            "Street light not working near the school", CITY_BENGALURU
        )
        self.assertEqual(result.category, models.IssueCategory.STREETLIGHT)
        self.assertFalse(result.needs_review)


if __name__ == "__main__":
    unittest.main(verbosity=2)
