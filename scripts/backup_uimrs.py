#!/usr/bin/env python
"""Create a transactionally safe, timestamped UIMRS backup.

The backup contains the SQLite database (via the SQLite backup API), the
uploaded evidence directory and a manifest with SHA-256 hashes. It never
overwrites an existing backup directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import resolve_database_url
from app.db_guard import sqlite_path_from_url, check_sqlite_integrity, validate_schema, REQUIRED_SCHEMA
from app.db_migrate import current_revision, HEAD_REVISION


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def database_revision(db_path: Path) -> str | None:
    try:
        connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def file_inventory(root: Path) -> list[dict]:
    if not root.exists():
        return []
    result = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        result.append(
            {
                "relative_path": path.relative_to(root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return result


def unique_backup_dir(output_root: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    candidate = output_root / f"uimrs_backup_{timestamp}"
    suffix = 1
    while candidate.exists():
        candidate = output_root / f"uimrs_backup_{timestamp}_{suffix}"
        suffix += 1
    return candidate


def create_backup(database_url: str, upload_dir: str | Path, output_root: str | Path) -> Path:
    db_path = sqlite_path_from_url(database_url)
    if db_path is None:
        raise ValueError("Backup supports SQLite database URLs only.")
    if not db_path.exists():
        raise FileNotFoundError(f"Database does not exist: {db_path}")

    output_root = Path(output_root).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    backup_dir = unique_backup_dir(output_root)
    backup_dir.mkdir(parents=True, exist_ok=False)

    # Transactionally safe SQLite backup. This captures committed data even when
    # WAL mode is in use.
    backup_db = backup_dir / db_path.name
    source = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    destination = sqlite3.connect(str(backup_db))
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()

    integrity = check_sqlite_integrity(database_url)
    if integrity.lower() != "ok":
        raise RuntimeError(f"Source database integrity check failed: {integrity}")

    try:
        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{backup_db.as_posix()}")
        validate_schema(engine, REQUIRED_SCHEMA)
    except Exception as exc:
        raise RuntimeError(f"Backed-up database schema validation failed: {exc}") from exc

    revision = database_revision(backup_db)
    if revision != HEAD_REVISION:
        raise RuntimeError(
            f"Backed-up database revision {revision!r} does not match "
            f"expected revision {HEAD_REVISION!r}."
        )

    upload_root = Path(upload_dir).expanduser().resolve()
    backup_uploads = backup_dir / "uploads"
    if upload_root.exists():
        shutil.copytree(upload_root, backup_uploads)
    else:
        backup_uploads.mkdir(parents=True, exist_ok=True)

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "application": "UIMRS",
        "alembic_revision": revision,
        "database": {
            "file": db_path.name,
            "size_bytes": backup_db.stat().st_size,
            "sha256": sha256_file(backup_db),
            "integrity_check": integrity,
        },
        "uploads": {
            "root": "uploads",
            "files": file_inventory(backup_uploads),
        },
    }
    manifest_path = backup_dir / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")

    # Verify the manifest's recorded hashes.
    if sha256_file(backup_db) != manifest["database"]["sha256"]:
        raise RuntimeError("Backup database hash verification failed.")
    for entry in manifest["uploads"]["files"]:
        path = backup_uploads / entry["relative_path"]
        if not path.is_file() or sha256_file(path) != entry["sha256"]:
            raise RuntimeError(f"Backup evidence hash verification failed: {entry['relative_path']}")

    return backup_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a timestamped UIMRS backup.")
    parser.add_argument("--database-url", default=None, help="SQLAlchemy database URL. Defaults to application configuration.")
    parser.add_argument("--upload-dir", default=None, help="Upload/evidence directory. Defaults to application configuration.")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "backups"), help="Directory that will contain the timestamped backup.")
    args = parser.parse_args()

    database_url = args.database_url or resolve_database_url()
    settings_upload_dir = None
    if args.upload_dir is None:
        try:
            from app.core.config import get_settings

            settings_upload_dir = get_settings().UPLOAD_DIR
        except Exception:
            settings_upload_dir = str(PROJECT_ROOT / "uploads")
    upload_dir = args.upload_dir or settings_upload_dir or str(PROJECT_ROOT / "uploads")

    try:
        backup_dir = create_backup(database_url, upload_dir, args.output_dir)
    except Exception as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1

    print(f"Backup created: {backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
