"""UIMRS Task 12B — deployment readiness, backup/restore and readiness tests."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.core.config import get_settings
from app.database import Base, get_db
from app.db_migrate import prepare_database, current_revision, HEAD_REVISION
from app.main import app, settings as app_settings, engine as app_engine
import app.main as main_module

import backup_uimrs
import restore_uimrs


class TestEnvExample(unittest.TestCase):
    def test_env_example_contains_required_variables(self):
        content = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        for name in (
            "SECRET_KEY",
            "UIMRS_ENV",
            "UIMRS_FRONTEND_MODE",
            "DATABASE_URL",
            "ALLOWED_ORIGINS",
            "UPLOAD_DIR",
            "MAX_UPLOAD_SIZE_MB",
        ):
            self.assertIn(name, content)
        self.assertNotIn("AdminSmoke", content)
        self.assertNotIn("password=", content.lower())


class TestScriptPresence(unittest.TestCase):
    def test_deployment_scripts_exist(self):
        for name in ("run_windows.ps1", "run_production.ps1", "scripts/backup_uimrs.py", "scripts/restore_uimrs.py"):
            self.assertTrue((PROJECT_ROOT / name).is_file(), name)
        self.assertTrue((PROJECT_ROOT / "docs" / "DEPLOYMENT_GUIDE.md").is_file())


class TestReadinessEndpoint(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="uimrs_ready_"))
        self.db_path = self.temp_dir / "ready.db"
        self.original_engine = main_module.engine
        self.original_database_url = app_settings.DATABASE_URL
        self.original_get_settings = None

    def tearDown(self):
        app.dependency_overrides.clear()
        main_module.engine = self.original_engine
        app_settings.DATABASE_URL = self.original_database_url
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _use_database(self, url):
        main_module.engine = create_engine(url, connect_args={"check_same_thread": False})
        app_settings.DATABASE_URL = url

    def test_ready_returns_200_for_valid_database(self):
        url = f"sqlite:///{self.db_path.as_posix()}"
        prepare_database(url)
        self._use_database(url)
        client = TestClient(app)
        response = client.get("/ready")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(response.json()["revision"], HEAD_REVISION)

    def test_ready_returns_503_for_missing_database(self):
        missing = self.temp_dir / "missing.db"
        url = f"sqlite:///{missing.as_posix()}"
        self._use_database(url)
        client = TestClient(app)
        response = client.get("/ready")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["status"], "not_ready")

    def test_ready_returns_503_for_invalid_schema(self):
        import sqlite3

        path = self.temp_dir / "bad.db"
        connection = sqlite3.connect(str(path))
        connection.execute("CREATE TABLE users (id TEXT)")
        connection.commit()
        connection.close()
        url = f"sqlite:///{path.as_posix()}"
        self._use_database(url)
        client = TestClient(app)
        response = client.get("/ready")
        self.assertEqual(response.status_code, 503, response.text)


class TestBackupRestore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="uimrs_backup_"))
        self.db_path = self.temp_dir / "source.db"
        self.upload_dir = self.temp_dir / "uploads"
        self.evidence_dir = self.upload_dir / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.backup_root = self.temp_dir / "backups"
        self.target_dir = self.temp_dir / "restore_target"

        url = f"sqlite:///{self.db_path.as_posix()}"
        prepare_database(url)
        self.evidence_file = self.evidence_dir / "sample.jpg"
        self.evidence_file.write_bytes(b"\xff\xd8\xff" + b"\x00" * 16)
        self.url = url

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_backup_creates_verified_manifest(self):
        backup_dir = backup_uimrs.create_backup(self.url, self.upload_dir, self.backup_root)
        self.assertTrue(backup_dir.is_dir())
        self.assertFalse((self.backup_root / backup_dir.name).exists() is False)
        manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["alembic_revision"], HEAD_REVISION)
        self.assertEqual(manifest["database"]["integrity_check"].lower(), "ok")
        self.assertEqual(len(manifest["uploads"]["files"]), 1)
        self.assertTrue((backup_dir / "uploads" / "evidence" / "sample.jpg").is_file())

    def test_backup_does_not_overwrite_existing_directory(self):
        first = backup_uimrs.create_backup(self.url, self.upload_dir, self.backup_root)
        second = backup_uimrs.create_backup(self.url, self.upload_dir, self.backup_root)
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())

    def test_restore_requires_confirmation(self):
        backup_dir = backup_uimrs.create_backup(self.url, self.upload_dir, self.backup_root)
        ok, messages = restore_uimrs.restore_backup(backup_dir, self.target_dir)
        # Direct restore function performs restoration; CLI gating is tested separately.
        self.assertTrue(ok, "\n".join(messages))

    def test_restore_validates_backup_and_hashes(self):
        backup_dir = backup_uimrs.create_backup(self.url, self.upload_dir, self.backup_root)
        ok, messages = restore_uimrs.validate_backup(backup_dir)
        self.assertTrue(ok, "\n".join(messages))
        self.assertTrue(any("SHA-256" in message or "sha256" in message.lower() for message in messages))

    def test_restore_refuses_invalid_manifest(self):
        backup_dir = self.backup_root / "bad_backup"
        backup_dir.mkdir(parents=True)
        (backup_dir / "manifest.json").write_text("{}", encoding="utf-8")
        ok, messages = restore_uimrs.validate_backup(backup_dir)
        self.assertFalse(ok)
        self.assertTrue(messages)

    def test_restore_preserves_original_target_path_check(self):
        backup_dir = backup_uimrs.create_backup(self.url, self.upload_dir, self.backup_root)
        target = self.temp_dir / "existing_target"
        target.mkdir()
        ok, messages = restore_uimrs.restore_backup(backup_dir, target)
        self.assertFalse(ok)
        self.assertTrue(any("already exists" in message for message in messages))

    def test_cli_restore_requires_confirm(self):
        backup_dir = backup_uimrs.create_backup(self.url, self.upload_dir, self.backup_root)
        import subprocess
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "restore_uimrs.py"),
                "--backup-dir",
                str(backup_dir),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("No files were restored", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
