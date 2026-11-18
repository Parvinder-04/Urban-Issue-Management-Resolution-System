"""
UIMRS Task 1 — Database configuration, schema guard and working-copy safety.

Every test runs against synthetic databases inside a temporary directory. The
public local database (``local_data/uimrs_public.db``) is never opened or
modified.
"""
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine

from app.core.config import (
    CANDIDATE_DB_PATH,
    DEV_DB_PATH,
    LEGACY_DB_PATH,
    PROJECT_ROOT as CONFIG_PROJECT_ROOT,
    Settings,
    default_database_url,
    resolve_database_url,
)
from app.db_guard import (
    REQUIRED_SCHEMA,
    DatabaseIntegrityError,
    DatabaseSchemaError,
    check_sqlite_integrity,
    guard_startup,
    safe_backup_database,
    sqlite_path_from_url,
    validate_schema,
)


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{Path(path).as_posix()}"


def create_tables(db_path: Path, schema=REQUIRED_SCHEMA) -> None:
    """Create exactly the given tables/columns in a synthetic database."""
    con = sqlite3.connect(str(db_path))
    try:
        for table, columns in schema.items():
            cols = ", ".join(f'"{c}" TEXT' for c in columns)
            con.execute(f'CREATE TABLE "{table}" ({cols})')
        con.commit()
    finally:
        con.close()


def file_fingerprint(path: Path):
    stat = path.stat()
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        stat.st_size,
        stat.st_mtime_ns,
    )


class TestDatabaseUrlSelection(unittest.TestCase):
    def test_default_points_at_separate_development_database(self):
        url = default_database_url()
        self.assertEqual(url, sqlite_url(DEV_DB_PATH))
        self.assertTrue(url.endswith("uimrs_public.db"))
        self.assertEqual(sqlite_path_from_url(url), DEV_DB_PATH)

    def test_default_never_selects_protected_databases(self):
        selected = Path(sqlite_path_from_url(default_database_url())).resolve()
        self.assertNotEqual(selected, LEGACY_DB_PATH.resolve())
        self.assertNotEqual(selected, CANDIDATE_DB_PATH.resolve())
        self.assertEqual(DEV_DB_PATH.name, "uimrs_public.db")

    def test_default_path_is_project_rooted_not_deployment_specific(self):
        selected = Path(sqlite_path_from_url(default_database_url()))
        self.assertTrue(selected.is_absolute())
        self.assertEqual(
            selected.parent.resolve(), (CONFIG_PROJECT_ROOT / "local_data").resolve()
        )
        self.assertNotIn("postgres", default_database_url().lower())

    def test_environment_override_wins(self):
        override = "sqlite:///D:/deployments/uimrs/data/prod.db"
        self.assertEqual(
            resolve_database_url({"DATABASE_URL": override}), override
        )

    def test_blank_environment_value_falls_back_to_default(self):
        self.assertEqual(resolve_database_url({"DATABASE_URL": "   "}), default_database_url())
        self.assertEqual(resolve_database_url({}), default_database_url())

    def test_non_sqlite_url_is_not_treated_as_a_local_file(self):
        self.assertIsNone(
            sqlite_path_from_url("postgresql://user:secret@db-host:5432/uimrs")
        )
        self.assertIsNone(sqlite_path_from_url("sqlite:///:memory:"))

    @unittest.skipIf(
        "DATABASE_URL" in os.environ,
        "DATABASE_URL is set in this shell; import-time settings cannot be asserted",
    )
    def test_settings_use_the_resolved_default(self):
        self.assertEqual(Settings.DATABASE_URL, resolve_database_url())


class TestSchemaGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "schema_test.db"

    def engine(self):
        return create_engine(sqlite_url(self.db_path))

    def test_complete_schema_is_accepted(self):
        create_tables(self.db_path)
        engine = self.engine()
        self.addCleanup(engine.dispose)
        self.assertEqual(guard_startup(engine, sqlite_url(self.db_path)), "validated")
        validate_schema(engine)  # must not raise

    def test_missing_table_is_detected(self):
        partial = {name: cols for name, cols in REQUIRED_SCHEMA.items() if name != "issues"}
        create_tables(self.db_path, partial)
        engine = self.engine()
        self.addCleanup(engine.dispose)
        with self.assertRaises(DatabaseSchemaError) as ctx:
            guard_startup(engine, sqlite_url(self.db_path))
        message = str(ctx.exception)
        self.assertIn("missing table: issues", message)
        self.assertIn("No schema was changed automatically", message)

    def test_missing_required_columns_are_detected(self):
        columns = [c for c in REQUIRED_SCHEMA["issues"] if c not in {"city", "is_historical"}]
        schema = dict(REQUIRED_SCHEMA)
        schema["issues"] = tuple(columns)
        create_tables(self.db_path, schema)
        engine = self.engine()
        self.addCleanup(engine.dispose)
        with self.assertRaises(DatabaseSchemaError) as ctx:
            validate_schema(engine)
        message = str(ctx.exception)
        self.assertIn("table 'issues' is missing columns: city, is_historical", message)

    def test_legacy_nyc_shaped_issues_table_is_rejected(self):
        """A schema without India provenance columns must not be usable."""
        schema = dict(REQUIRED_SCHEMA)
        schema["issues"] = tuple(
            c for c in REQUIRED_SCHEMA["issues"]
            if c not in {"city", "is_historical", "source_dataset",
                         "source_record_id", "source_agency"}
        )
        create_tables(self.db_path, schema)
        engine = self.engine()
        self.addCleanup(engine.dispose)
        with self.assertRaises(DatabaseSchemaError):
            guard_startup(engine, sqlite_url(self.db_path))

    def test_empty_database_is_reported_as_uninitialised(self):
        engine = self.engine()
        self.addCleanup(engine.dispose)
        self.assertEqual(
            guard_startup(engine, sqlite_url(self.db_path)), "uninitialized"
        )


class TestIntegrityCheck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "integrity.db"

    def test_healthy_database_reports_ok(self):
        create_tables(self.db_path)
        self.assertEqual(check_sqlite_integrity(sqlite_url(self.db_path)), "ok")

    def test_corrupt_database_raises(self):
        self.db_path.write_bytes(b"this is not a sqlite database" * 64)
        with self.assertRaises(DatabaseIntegrityError) as ctx:
            check_sqlite_integrity(sqlite_url(self.db_path))
        self.assertIn("integrity check failed", str(ctx.exception).lower())

    def test_missing_database_file_raises(self):
        missing = Path(self._tmp.name) / "absent.db"
        with self.assertRaises(DatabaseIntegrityError):
            check_sqlite_integrity(sqlite_url(missing))

    def test_corrupt_database_fails_startup_guard_before_schema_errors(self):
        # Two tables exist so the guard proceeds to the integrity stage.
        create_tables(self.db_path)
        con = sqlite3.connect(str(self.db_path))
        con.execute('CREATE TABLE extra (id TEXT)')
        con.commit()
        con.close()
        # Overwrite the header to corrupt the file while keeping it non-empty.
        raw = bytearray(self.db_path.read_bytes())
        raw[0:16] = b"\x00" * 16
        self.db_path.write_bytes(bytes(raw))
        engine = create_engine(sqlite_url(self.db_path))
        self.addCleanup(engine.dispose)
        with self.assertRaises(DatabaseIntegrityError):
            guard_startup(engine, sqlite_url(self.db_path))

    def test_non_sqlite_url_skips_file_integrity_check(self):
        self.assertIsNone(check_sqlite_integrity("postgresql://host/db"))


class TestSafeWorkingCopy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.source = self.tmp / "candidate_source.db"
        self.destination = self.tmp / "dev_copy.db"

    def _seed_source(self):
        create_tables(self.source)
        con = sqlite3.connect(str(self.source))
        try:
            con.execute(
                "INSERT INTO issues (id, city, is_historical, source_record_id, "
                "priority_score, resolved_at) VALUES "
                "('i-1', 'Bengaluru, Karnataka, India', '1', 'src-1', NULL, NULL),"
                "('i-2', 'Bengaluru, Karnataka, India', '1', 'src-2', NULL, NULL)"
            )
            con.commit()
        finally:
            con.close()

    def test_historical_rows_are_preserved_exactly(self):
        self._seed_source()
        safe_backup_database(self.source, self.destination)

        con = sqlite3.connect(str(self.destination))
        try:
            rows = con.execute(
                "SELECT id, city, is_historical, source_record_id, priority_score, "
                "resolved_at FROM issues ORDER BY id"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][1], "Bengaluru, Karnataka, India")
            self.assertEqual(rows[0][2], "1")
            self.assertIsNone(rows[0][4])
            self.assertIsNone(rows[0][5])
        finally:
            con.close()

        src_row = self._table_digest(self.source)
        dst_row = self._table_digest(self.destination)
        self.assertEqual(src_row, dst_row)
        self.assertEqual(check_sqlite_integrity(sqlite_url(self.destination)), "ok")

    @staticmethod
    def _table_digest(path: Path) -> str:
        con = sqlite3.connect(str(path))
        try:
            digest = hashlib.sha256()
            for row in con.execute("SELECT * FROM issues ORDER BY id").fetchall():
                digest.update(repr(row).encode("utf-8"))
            return digest.hexdigest()
        finally:
            con.close()

    def test_existing_destination_is_never_overwritten(self):
        self._seed_source()
        self.destination.write_bytes(b"existing work I must not lose")
        before = file_fingerprint(self.destination)
        with self.assertRaises(FileExistsError):
            safe_backup_database(self.source, self.destination)
        self.assertEqual(file_fingerprint(self.destination), before)

    def test_missing_source_is_rejected(self):
        with self.assertRaises(FileNotFoundError):
            safe_backup_database(self.tmp / "absent.db", self.destination)

    def test_source_is_not_modified_by_backup(self):
        self._seed_source()
        before_source = file_fingerprint(self.source)
        safe_backup_database(self.source, self.destination)
        self.assertEqual(file_fingerprint(self.source), before_source)

    def test_guard_and_integrity_checks_are_read_only(self):
        self._seed_source()
        before = file_fingerprint(self.source)
        engine = create_engine(sqlite_url(self.source))
        self.addCleanup(engine.dispose)
        self.assertEqual(guard_startup(engine, sqlite_url(self.source)), "validated")
        self.assertEqual(check_sqlite_integrity(sqlite_url(self.source)), "ok")
        self.assertEqual(file_fingerprint(self.source), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
