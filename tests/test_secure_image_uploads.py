"""UIMRS Task 7 — secure evidence storage and upload validation tests.

Covers magic-byte detection, size limits, corrupt/unsupported content, MIME
spoofing, server-generated filenames, traversal-safe resolution, cleanup after
database failure and authenticated retrieval scoping.

All storage happens in a temporary directory; the database is isolated in
memory. No real project database or upload directory is touched.
"""
import re
import os
import shutil
import sys
import tempfile
import unittest
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
from app.services.evidence_storage import (
    EvidenceStorageError,
    EvidenceTooLargeError,
    UnsupportedImageTypeError,
    delete_evidence,
    detect_image_format,
    resolve_evidence_path,
    store_evidence,
)
from app.services.image_verification_service import verify_image_bytes

TEST_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

TEST_PASSWORD = "SecureUploads#2026"


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


class TestEvidenceStorageUnit(unittest.TestCase):
    def setUp(self):
        self.settings = get_settings()
        self.original_upload_dir = self.settings.UPLOAD_DIR
        self.original_max_size = self.settings.MAX_UPLOAD_SIZE_MB
        self.upload_dir = tempfile.mkdtemp(prefix="uimrs_storage_unit_")
        self.settings.UPLOAD_DIR = self.upload_dir

    def tearDown(self):
        self.settings.UPLOAD_DIR = self.original_upload_dir
        self.settings.MAX_UPLOAD_SIZE_MB = self.original_max_size
        shutil.rmtree(self.upload_dir, ignore_errors=True)

    def test_detects_supported_magic_bytes(self):
        self.assertEqual(detect_image_format(make_image_bytes(".jpg")), "jpeg")
        self.assertEqual(detect_image_format(make_image_bytes(".png")), "png")
        self.assertEqual(detect_image_format(make_image_bytes(".webp")), "webp")

    def test_rejects_svg_html_executable_and_empty(self):
        payloads = [
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            b"<!doctype html><html><body>not an image</body></html>",
            b"MZ\x90\x00\x03\x00\x00\x00",
            b"",
        ]
        for payload in payloads:
            self.assertIsNone(detect_image_format(payload))

    def test_store_uses_uuid_and_relative_key(self):
        data = make_image_bytes(".png")
        stored = store_evidence(data, "png")
        self.assertRegex(stored.storage_key, r"^evidence/[0-9a-f]{32}\.png$")
        self.assertTrue(stored.path.is_file())
        self.assertEqual(stored.path.read_bytes(), data)
        self.assertEqual(stored.media_type, "image/png")
        self.assertEqual(stored.path.parent, (Path(self.upload_dir) / "evidence").resolve())

    def test_store_rejects_oversized_payload(self):
        self.settings.MAX_UPLOAD_SIZE_MB = 1
        oversized = b"\xff\xd8\xff" + (b"0" * (1024 * 1024 + 1))
        with self.assertRaises(EvidenceTooLargeError):
            store_evidence(oversized, "jpeg")

    def test_store_rejects_format_mismatch(self):
        with self.assertRaises(UnsupportedImageTypeError):
            store_evidence(make_image_bytes(".jpg"), "png")

    def test_store_rejects_unsupported_format(self):
        with self.assertRaises(UnsupportedImageTypeError):
            store_evidence(b"GIF89a" + b"0" * 20, "gif")

    def test_resolve_rejects_traversal_and_absolute_paths(self):
        unsafe = [
            "../secret.jpg",
            "evidence/../secret.jpg",
            "evidence/../../secret.jpg",
            "evidence/nested/file.jpg",
            "/etc/passwd",
            "C:\\Windows\\win.ini",
            "C:/Windows/win.ini",
            "evidence/",
            "other/file.jpg",
            "evidence/file.txt",
            "",
        ]
        for key in unsafe:
            with self.assertRaises(EvidenceStorageError, msg=key):
                resolve_evidence_path(key)

    def test_resolve_accepts_generated_key(self):
        stored = store_evidence(make_image_bytes(".jpg"), "jpeg")
        resolved = resolve_evidence_path(stored.storage_key)
        self.assertEqual(resolved, stored.path)

    def test_delete_removes_file(self):
        stored = store_evidence(make_image_bytes(".jpg"), "jpeg")
        self.assertTrue(stored.path.is_file())
        self.assertTrue(delete_evidence(stored.storage_key))
        self.assertFalse(stored.path.exists())
        self.assertFalse(delete_evidence(stored.storage_key))

    def test_corrupt_image_is_not_decodable(self):
        corrupt = b"\xff\xd8\xff" + (b"not-a-real-jpeg" * 20)
        result = verify_image_bytes(corrupt)
        self.assertFalse(result.passed)
        self.assertEqual(result.resolution, (0, 0))
        self.assertIn("Could not read image file", result.issues)

    def test_valid_image_decodes(self):
        result = verify_image_bytes(make_image_bytes(".webp"))
        self.assertTrue(result.passed)
        self.assertEqual(result.resolution, (400, 400))


class TestSecureUploadEndpoints(unittest.TestCase):
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
        self.original_max_size = self.settings.MAX_UPLOAD_SIZE_MB
        self.upload_dir = tempfile.mkdtemp(prefix="uimrs_secure_uploads_")
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
        self.settings.MAX_UPLOAD_SIZE_MB = self.original_max_size
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
            "title": "Secure upload fixture",
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
            name="TEST_UPLOAD_ROADS",
            handles_category=models.IssueCategory.ROAD_DAMAGE,
            city="Gurugram, Haryana, India",
        )
        self.dept_garbage = models.Department(
            name="TEST_UPLOAD_GARBAGE",
            handles_category=models.IssueCategory.GARBAGE_WASTE,
            city="Gurugram, Haryana, India",
        )
        self.db.add_all([self.dept_roads, self.dept_garbage])
        self.db.commit()

        self.citizen_a = self._user("Upload Alpha", "upload.alpha@example.com", models.UserRole.CITIZEN)
        self.citizen_b = self._user("Upload Beta", "upload.beta@example.com", models.UserRole.CITIZEN)
        self.officer_roads = self._user(
            "Upload Road Officer", "upload.roads@example.com", models.UserRole.OFFICER, self.dept_roads.id
        )
        self.officer_garbage = self._user(
            "Upload Garbage Officer", "upload.garbage@example.com", models.UserRole.OFFICER, self.dept_garbage.id
        )
        self.admin = self._user("Upload Admin", "upload.admin@example.com", models.UserRole.ADMIN)
        self.db.add_all([self.citizen_a, self.citizen_b, self.officer_roads, self.officer_garbage, self.admin])
        self.db.commit()

        self.token_a = create_access_token(self.citizen_a)
        self.token_b = create_access_token(self.citizen_b)
        self.token_roads = create_access_token(self.officer_roads)
        self.token_garbage = create_access_token(self.officer_garbage)
        self.token_admin = create_access_token(self.admin)

        self.issue_a = self._issue("SECURE-UPLOAD-001", self.citizen_a, self.dept_roads)
        self.historical = self._issue(
            "SECURE-UPLOAD-HIST-001",
            self.citizen_a,
            self.dept_roads,
            is_historical=True,
            source_record_id="SECURE-HIST-001",
        )

    def _upload(self, issue_id, token, data, filename="evidence.jpg", content_type="image/jpeg"):
        return self.client.post(
            f"/api/v1/issues/{issue_id}/images",
            files={"file": (filename, data, content_type)},
            headers=bearer(token),
        )

    def _stored_files(self):
        evidence_dir = Path(self.upload_dir) / "evidence"
        if not evidence_dir.exists():
            return []
        return [path for path in evidence_dir.iterdir() if path.is_file()]

    def test_valid_jpeg_upload_is_stored_securely(self):
        data = make_image_bytes(".jpg")
        response = self._upload(self.issue_a.id, self.token_a, data)
        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertRegex(payload["image_url"], r"^evidence/[0-9a-f]{32}\.jpg$")
        self.assertTrue(payload["passed_verification"])
        self.assertEqual(len(self._stored_files()), 1)

    def test_original_filename_is_never_used(self):
        response = self._upload(
            self.issue_a.id,
            self.token_a,
            make_image_bytes(".jpg"),
            filename="../../outside/evil-name.jpg",
        )
        self.assertEqual(response.status_code, 201)
        key = response.json()["image_url"]
        self.assertNotIn("evil-name", key)
        self.assertNotIn("outside", key)
        self.assertNotIn("..", key)
        self.assertFalse((Path(self.upload_dir).parent / "outside").exists())

    def test_mime_spoofed_html_is_rejected(self):
        response = self._upload(
            self.issue_a.id,
            self.token_a,
            b"<!doctype html><html><body>script</body></html>",
            filename="photo.jpg",
            content_type="image/jpeg",
        )
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self._stored_files(), [])

    def test_png_labelled_as_jpeg_is_stored_as_png(self):
        data = make_image_bytes(".png")
        response = self._upload(
            self.issue_a.id, self.token_a, data, filename="photo.jpg", content_type="image/jpeg"
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["image_url"].endswith(".png"))

        image_id = response.json()["id"]
        content = self.client.get(
            f"/api/v1/issues/{self.issue_a.id}/images/{image_id}/content",
            headers=bearer(self.token_a),
        )
        self.assertEqual(content.status_code, 200)
        self.assertIn("image/png", content.headers.get("content-type", ""))

    def test_svg_is_rejected(self):
        response = self._upload(
            self.issue_a.id,
            self.token_a,
            b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>',
            filename="evidence.svg",
            content_type="image/svg+xml",
        )
        self.assertEqual(response.status_code, 415)

    def test_executable_is_rejected(self):
        response = self._upload(
            self.issue_a.id,
            self.token_a,
            b"MZ\x90\x00\x03\x00\x00\x00fake-executable",
            filename="evidence.exe",
            content_type="application/octet-stream",
        )
        self.assertEqual(response.status_code, 415)

    def test_corrupt_image_is_rejected(self):
        response = self._upload(
            self.issue_a.id,
            self.token_a,
            b"\xff\xd8\xff" + (b"broken-data" * 50),
            filename="corrupt.jpg",
            content_type="image/jpeg",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("not a valid decodable image", response.json()["detail"])
        self.assertEqual(self._stored_files(), [])

    def test_oversized_upload_is_rejected(self):
        self.settings.MAX_UPLOAD_SIZE_MB = 1
        oversized = b"\xff\xd8\xff" + (b"0" * (1024 * 1024 + 1))
        response = self._upload(self.issue_a.id, self.token_a, oversized)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self._stored_files(), [])

    def test_citizen_cannot_upload_to_another_citizens_issue(self):
        response = self._upload(self.issue_a.id, self.token_b, make_image_bytes(".jpg"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._stored_files(), [])

    def test_historical_upload_rejected_before_storage(self):
        response = self._upload(self.historical.id, self.token_a, make_image_bytes(".jpg"))
        self.assertEqual(response.status_code, 403)
        self.assertIn("historical", response.json()["detail"].lower())
        self.assertEqual(self._stored_files(), [])

    def test_unauthorised_retrieval_is_blocked(self):
        upload = self._upload(self.issue_a.id, self.token_a, make_image_bytes(".jpg"))
        self.assertEqual(upload.status_code, 201)
        image_id = upload.json()["id"]

        citizen_b = self.client.get(
            f"/api/v1/issues/{self.issue_a.id}/images/{image_id}/content",
            headers=bearer(self.token_b),
        )
        self.assertEqual(citizen_b.status_code, 404)

        anonymous = self.client.get(
            f"/api/v1/issues/{self.issue_a.id}/images/{image_id}/content"
        )
        self.assertEqual(anonymous.status_code, 401)

    def test_cross_department_retrieval_is_blocked(self):
        upload = self._upload(self.issue_a.id, self.token_a, make_image_bytes(".jpg"))
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

        admin = self.client.get(
            f"/api/v1/issues/{self.issue_a.id}/images/{image_id}/content",
            headers=bearer(self.token_admin),
        )
        self.assertEqual(admin.status_code, 200)

    def test_legacy_absolute_storage_path_is_not_served(self):
        legacy = Path(self.upload_dir) / "legacy-evidence.jpg"
        legacy.write_bytes(make_image_bytes(".jpg"))
        image = models.IssueImage(
            issue_id=self.issue_a.id,
            image_url=str(legacy.resolve()),
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

    def test_database_failure_cleans_up_stored_file(self):
        def failing_get_db():
            db = TestingSessionLocal()

            def fail_commit():
                raise RuntimeError("simulated commit failure")

            db.commit = fail_commit
            try:
                yield db
            finally:
                db.close()

        original_override = app.dependency_overrides.get(get_db)
        app.dependency_overrides[get_db] = failing_get_db
        try:
            response = self._upload(self.issue_a.id, self.token_a, make_image_bytes(".jpg"))
        finally:
            app.dependency_overrides[get_db] = original_override

        self.assertEqual(response.status_code, 500)
        self.assertEqual(self._stored_files(), [])
        self.assertEqual(self.db.query(models.IssueImage).count(), 0)


class TestDatabaseBackedImageStorage(unittest.TestCase):
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
        self.old_backend = os.environ.get("EVIDENCE_STORAGE_BACKEND")
        os.environ["EVIDENCE_STORAGE_BACKEND"] = "database"

        self.db.query(models.IssueStatusUpdate).delete()
        self.db.query(models.IssueImage).delete()
        self.db.query(models.Issue).delete()
        self.db.query(models.User).delete()
        self.db.query(models.Department).delete()
        self.db.commit()

        self.dept = models.Department(
            name="DB_IMAGE_ROADS",
            handles_category=models.IssueCategory.ROAD_DAMAGE,
            city="Gurugram, Haryana, India",
        )
        self.db.add(self.dept)
        self.db.commit()

        self.citizen = models.User(
            name="DB Image Citizen",
            email="db.image.citizen@example.com",
            password_hash=self.password_hash,
            role=models.UserRole.CITIZEN,
        )
        self.other = models.User(
            name="DB Image Other",
            email="db.image.other@example.com",
            password_hash=self.password_hash,
            role=models.UserRole.CITIZEN,
        )
        self.admin = models.User(
            name="DB Image Admin",
            email="db.image.admin@example.com",
            password_hash=self.password_hash,
            role=models.UserRole.ADMIN,
        )
        self.db.add_all([self.citizen, self.other, self.admin])
        self.db.commit()

        self.token = create_access_token(self.citizen)
        self.other_token = create_access_token(self.other)
        self.admin_token = create_access_token(self.admin)

        self.issue = models.Issue(
            ticket_number="DB-IMAGE-001",
            title="Database image fixture",
            description="Fixture for database-backed image tests.",
            category=models.IssueCategory.ROAD_DAMAGE,
            category_confidence=0.0,
            category_source="india_keyword_rule",
            priority_score=1.0,
            priority_level=models.PriorityLevel.LOW,
            status=models.IssueStatus.REPORTED,
            latitude=28.4595,
            longitude=77.0266,
            city="Gurugram, Haryana, India",
            reporter_id=self.citizen.id,
            department_id=self.dept.id,
            is_historical=False,
        )
        self.db.add(self.issue)
        self.db.commit()
        self.db.refresh(self.issue)

    def tearDown(self):
        self.db.close()
        app.dependency_overrides = self.old_overrides
        if self.old_backend is None:
            os.environ.pop("EVIDENCE_STORAGE_BACKEND", None)
        else:
            os.environ["EVIDENCE_STORAGE_BACKEND"] = self.old_backend

    def _upload(self, data, filename="evidence.jpg", content_type="image/jpeg"):
        return self.client.post(
            f"/api/v1/issues/{self.issue.id}/images",
            files={"file": (filename, data, content_type)},
            headers=bearer(self.token),
        )

    def test_database_upload_stores_bytes_and_key(self):
        data = make_image_bytes(".jpg")
        response = self._upload(data)
        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertRegex(payload["image_url"], r"^db:[0-9a-f-]{36}\.jpg$")
        image = self.db.query(models.IssueImage).filter_by(id=payload["id"]).first()
        self.assertIsNotNone(image.image_data)
        self.assertGreater(len(image.image_data), 0)

    def test_database_upload_content_round_trip(self):
        data = make_image_bytes(".png")
        upload = self._upload(data, filename="photo.jpg", content_type="image/jpeg")
        self.assertEqual(upload.status_code, 201)
        image_id = upload.json()["id"]
        response = self.client.get(
            f"/api/v1/issues/{self.issue.id}/images/{image_id}/content",
            headers=bearer(self.token),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("image/png", response.headers.get("content-type", ""))
        self.assertGreater(len(response.content), 0)

    def test_database_upload_rejects_invalid_and_oversized(self):
        invalid = self._upload(b"\xff\xd8\xff" + b"broken" * 20)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(self.db.query(models.IssueImage).count(), 0)

        oversized = make_image_bytes(".jpg")
        # Force a tiny limit without changing the global default permanently.
        self.settings.MAX_UPLOAD_SIZE_MB = 0
        try:
            response = self._upload(oversized)
        finally:
            self.settings.MAX_UPLOAD_SIZE_MB = 10
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.db.query(models.IssueImage).count(), 0)

    def test_database_content_requires_authorisation(self):
        upload = self._upload(make_image_bytes(".jpg"))
        self.assertEqual(upload.status_code, 201)
        image_id = upload.json()["id"]

        anonymous = self.client.get(
            f"/api/v1/issues/{self.issue.id}/images/{image_id}/content"
        )
        self.assertEqual(anonymous.status_code, 401)

        other = self.client.get(
            f"/api/v1/issues/{self.issue.id}/images/{image_id}/content",
            headers=bearer(self.other_token),
        )
        self.assertEqual(other.status_code, 404)

        admin = self.client.get(
            f"/api/v1/issues/{self.issue.id}/images/{image_id}/content",
            headers=bearer(self.admin_token),
        )
        self.assertEqual(admin.status_code, 200)

    def test_database_image_survives_new_session(self):
        upload = self._upload(make_image_bytes(".jpg"))
        self.assertEqual(upload.status_code, 201)
        image_id = upload.json()["id"]

        fresh_db = TestingSessionLocal()
        try:
            image = fresh_db.query(models.IssueImage).filter_by(id=image_id).first()
            self.assertIsNotNone(image)
            self.assertIsNotNone(image.image_data)
            self.assertGreater(len(image.image_data), 0)
        finally:
            fresh_db.close()

if __name__ == "__main__":
    unittest.main()
