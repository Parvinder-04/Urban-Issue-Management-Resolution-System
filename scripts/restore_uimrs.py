#!/usr/bin/env python
"""Validate and prepare a UIMRS backup for restoration.

By default this script restores into a separate target directory and validates
the result. It never overwrites the live database or upload directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db_guard import sqlite_path_from_url, check_sqlite_integrity, validate_schema, REQUIRED_SCHEMA
from app.db_migrate import HEAD_REVISION


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_backup(backup_dir: Path) -> tuple[bool, list[str]]:
    messages = []
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        return False, ["manifest.json is missing."]

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, [f"manifest.json could not be read: {exc}"]

    db_file = manifest.get("database", {}).get("file")
    if not db_file:
        return False, ["Manifest does not name a database file."]

    backup_db = backup_dir / db_file
    if not backup_db.is_file():
        return False, [f"Backed-up database is missing: {db_file}"]

    integrity = check_sqlite_integrity(f"sqlite:///{backup_db.as_posix()}")
    if integrity.lower() != "ok":
        messages.append(f"SQLite integrity check failed: {integrity}")
    else:
        messages.append("SQLite integrity check: ok")

    try:
        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{backup_db.as_posix()}")
        validate_schema(engine, REQUIRED_SCHEMA)
        messages.append("Required schema: present")
    except Exception as exc:
        messages.append(f"Required schema validation failed: {exc}")
        return False, messages

    revision = None
    try:
        connection = sqlite3.connect(f"file:{backup_db.as_posix()}?mode=ro", uri=True)
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        revision = row[0] if row else None
        connection.close()
    except sqlite3.Error as exc:
        messages.append(f"Could not read Alembic revision: {exc}")
        return False, messages

    if revision != HEAD_REVISION:
        messages.append(f"Alembic revision mismatch: found {revision!r}, expected {HEAD_REVISION!r}")
        return False, messages
    messages.append(f"Alembic revision: {revision}")

    recorded_hash = manifest.get("database", {}).get("sha256")
    if recorded_hash != sha256_file(backup_db):
        messages.append("Database SHA-256 does not match manifest.")
        return False, messages
    messages.append("Database SHA-256: verified")

    recorded_size = manifest.get("database", {}).get("size_bytes")
    if recorded_size is not None and recorded_size != backup_db.stat().st_size:
        messages.append("Database size does not match manifest.")
        return False, messages

    backup_uploads = backup_dir / "uploads"
    failed_evidence = []
    for entry in manifest.get("uploads", {}).get("files", []):
        relative = entry.get("relative_path")
        if not relative:
            failed_evidence.append("Manifest contains an evidence entry without relative_path.")
            continue
        path = backup_uploads / relative
        if not path.is_file():
            failed_evidence.append(f"Missing evidence file: {relative}")
            continue
        if entry.get("size_bytes") is not None and entry["size_bytes"] != path.stat().st_size:
            failed_evidence.append(f"Size mismatch: {relative}")
            continue
        if entry.get("sha256") != sha256_file(path):
            failed_evidence.append(f"Hash mismatch: {relative}")
            continue
    if failed_evidence:
        messages.extend(failed_evidence)
        return False, messages
    messages.append(f"Evidence files verified: {len(manifest.get('uploads', {}).get('files', []))}")

    return True, messages


def restore_backup(backup_dir: Path, target_dir: Path) -> tuple[bool, list[str]]:
    messages = []
    ok, validation_messages = validate_backup(backup_dir)
    messages.extend(validation_messages)
    if not ok:
        return False, messages

    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    target_dir = target_dir.expanduser().resolve()
    if target_dir.exists():
        messages.append(f"Target directory already exists: {target_dir}")
        return False, messages

    target_db_dir = target_dir / "database"
    target_upload_dir = target_dir / "uploads"
    try:
        target_db_dir.mkdir(parents=True, exist_ok=False)
        target_upload_dir.mkdir(parents=True, exist_ok=False)

        source_db = backup_dir / manifest["database"]["file"]
        target_db = target_db_dir / source_db.name
        shutil.copy2(source_db, target_db)

        source_uploads = backup_dir / "uploads"
        if source_uploads.exists():
            for path in source_uploads.rglob("*"):
                if path.is_file():
                    relative = path.relative_to(source_uploads)
                    destination = target_upload_dir / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)

        # Re-check the restored copy.
        integrity = check_sqlite_integrity(f"sqlite:///{target_db.as_posix()}")
        if integrity.lower() != "ok":
            raise RuntimeError(f"Restored database integrity check failed: {integrity}")
        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{target_db.as_posix()}")
        validate_schema(engine, REQUIRED_SCHEMA)
        connection = sqlite3.connect(f"file:{target_db.as_posix()}?mode=ro", uri=True)
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        connection.close()
        if not revision or revision[0] != HEAD_REVISION:
            raise RuntimeError("Restored database has an unexpected Alembic revision.")

        messages.append(f"Restored database: {target_db}")
        messages.append(f"Restored uploads: {target_upload_dir}")
        messages.append("Restore validation: ok")
        return True, messages
    except Exception as exc:
        messages.append(f"Restore failed: {exc}")
        # Preserve any partial target for inspection, but do not touch originals.
        return False, messages


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and prepare a UIMRS backup for restoration.")
    parser.add_argument("--backup-dir", required=True, help="Path to a timestamped backup directory.")
    parser.add_argument("--target-dir", default=None, help="Separate directory for restored data. Defaults to restore_<timestamp> next to the backup.")
    parser.add_argument("--confirm", default="", help="Must be RESTORE to perform restoration.")
    args = parser.parse_args()

    backup_dir = Path(args.backup_dir).expanduser().resolve()
    if not backup_dir.is_dir():
        print(f"Backup directory does not exist: {backup_dir}", file=sys.stderr)
        return 1

    ok, messages = validate_backup(backup_dir)
    for message in messages:
        print(message)
    if not ok:
        return 1

    if args.confirm != "RESTORE":
        print("Validation succeeded. No files were restored.")
        print("Re-run with --confirm RESTORE to create a separate validated restore target.")
        return 0

    if args.target_dir:
        target_dir = Path(args.target_dir).expanduser().resolve()
    else:
        target_dir = backup_dir.parent / f"restore_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    ok, messages = restore_backup(backup_dir, target_dir)
    for message in messages:
        print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
