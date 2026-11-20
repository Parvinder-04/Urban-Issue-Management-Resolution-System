"""Import live UIMRS evaluation records into a production database.

This script is additive and idempotent. It imports only live complaint records
(``is_historical = 0``) and the related users, status history and audit records
required to preserve workflows. It never imports historical Bengaluru rows,
foreign datasets, synthetic/demo complaints beyond the existing labelled
evaluation records, or local database files.

The target database is selected by ``DATABASE_URL`` or ``--database-url``.
The source database defaults to the isolated test environment database and is
opened read-only.

Usage::

    DATABASE_URL=postgresql://... \
    python scripts/import_evaluation_data_to_production.py --dry-run

    DATABASE_URL=postgresql://... \
    python scripts/import_evaluation_data_to_production.py
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

DEFAULT_SOURCE = PROJECT_ROOT / "test_environment" / "uimrs_test.db"


class ImportSafetyError(RuntimeError):
    """Raised when an import would be unsafe or ambiguous."""




def parse_dt(value):
    """Convert a SQLite datetime string into a Python datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Import live UIMRS evaluation records.")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="Source SQLite database.")
    parser.add_argument("--database-url", default=None, help="Target SQLAlchemy database URL.")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without writing.")
    return parser.parse_args(argv)


def source_rows(source: Path, sql: str, params=()):
    con = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def main(argv=None):
    args = parse_args(argv)
    source = Path(args.source).expanduser().resolve()
    if not source.is_file():
        raise ImportSafetyError(f"Source database not found: {source}")

    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url
    if not os.environ.get("DATABASE_URL"):
        raise ImportSafetyError("DATABASE_URL is required for the target database.")

    target_url = os.environ["DATABASE_URL"]

    # Application modules are imported only after DATABASE_URL is fixed.
    from app import models
    from app.core.config import is_production_environment
    from app.database import SessionLocal, engine
    from app.db_guard import guard_startup
    from app.db_migrate import HEAD_REVISION, current_revision

    if not target_url.startswith(("postgresql://", "postgres://", "sqlite:///")):
        raise ImportSafetyError("Unsupported target database URL.")

    # Validate target schema without modifying it.
    state = guard_startup(engine, target_url)
    if state != "validated":
        raise ImportSafetyError(f"Target database is not validated (state={state}).")
    if current_revision(target_url) != HEAD_REVISION:
        raise ImportSafetyError(
            f"Target database revision {current_revision(target_url)} is not {HEAD_REVISION}."
        )

    live_issues = source_rows(source, "SELECT * FROM issues WHERE is_historical = 0")
    live_issue_ids = {row["id"] for row in live_issues}
    users = source_rows(source, "SELECT * FROM users")
    status_updates = source_rows(
        source,
        "SELECT * FROM issue_status_updates WHERE issue_id IN (SELECT id FROM issues WHERE is_historical = 0)",
    )
    audit_logs = source_rows(source, "SELECT * FROM admin_audit_logs")
    issue_images = source_rows(
        source,
        "SELECT * FROM issue_images WHERE issue_id IN (SELECT id FROM issues WHERE is_historical = 0)",
    )

    print("Import plan")
    print(f"  source: {source}")
    print(f"  target: {target_url}")
    print(f"  live issues: {len(live_issues)}")
    print(f"  users: {len(users)}")
    print(f"  status updates: {len(status_updates)}")
    print(f"  audit logs: {len(audit_logs)}")
    print(f"  issue images: {len(issue_images)}")
    print(f"  dry run: {args.dry_run}")

    if args.dry_run:
        return 0

    db = SessionLocal()
    try:
        # Import users first, preserving password hashes and roles.
        imported_users = 0
        skipped_users = 0
        for row in users:
            # The system service user is created by application bootstrap. Do not
            # import the source copy; live records do not depend on its id.
            if row["email"] == "system@uimrs.local":
                skipped_users += 1
                continue

            existing = db.query(models.User).filter(models.User.id == row["id"]).first()
            if existing is not None:
                skipped_users += 1
                continue
            email_conflict = (
                db.query(models.User)
                .filter(models.User.email == row["email"])
                .first()
            )
            if email_conflict is not None:
                raise ImportSafetyError(
                    f"Email conflict for user {row['email']}: existing id {email_conflict.id}, source id {row['id']}"
                )
            db.add(
                models.User(
                    id=row["id"],
                    name=row["name"],
                    email=row["email"],
                    phone=row["phone"],
                    password_hash=row["password_hash"],
                    role=row["role"],
                    department_id=row["department_id"],
                    preferred_language=row["preferred_language"],
                    created_at=parse_dt(row["created_at"]),
                )
            )
            imported_users += 1

        # Import live issues.
        imported_issues = 0
        skipped_issues = 0
        for row in live_issues:
            existing = db.query(models.Issue).filter(models.Issue.id == row["id"]).first()
            if existing is not None:
                skipped_issues += 1
                continue
            ticket_conflict = (
                db.query(models.Issue)
                .filter(models.Issue.ticket_number == row["ticket_number"])
                .first()
            )
            if ticket_conflict is not None:
                raise ImportSafetyError(
                    f"Ticket conflict for {row['ticket_number']}: existing id {ticket_conflict.id}, source id {row['id']}"
                )
            department_id = row["department_id"]
            if department_id is not None:
                department = db.query(models.Department).filter(models.Department.id == department_id).first()
                if department is None:
                    raise ImportSafetyError(
                        f"Missing department {department_id} for issue {row['ticket_number']}"
                    )
            db.add(
                models.Issue(
                    id=row["id"],
                    ticket_number=row["ticket_number"],
                    title=row["title"],
                    description=row["description"],
                    category=row["category"],
                    category_confidence=row["category_confidence"],
                    category_source=row["category_source"],
                    category_model_version=row["category_model_version"],
                    category_needs_review=row["category_needs_review"],
                    category_review_reason=row["category_review_reason"],
                    category_top_alternatives=row["category_top_alternatives"],
                    category_score_kind=row["category_score_kind"],
                    priority_score=row["priority_score"],
                    priority_level=row["priority_level"],
                    priority_reasons=row["priority_reasons"],
                    status=row["status"],
                    latitude=row["latitude"],
                    longitude=row["longitude"],
                    address_text=row["address_text"],
                    duplicate_of_issue_id=row["duplicate_of_issue_id"],
                    duplicate_count=row["duplicate_count"],
                    hotspot_cluster_id=row["hotspot_cluster_id"],
                    reporter_id=row["reporter_id"],
                    department_id=department_id,
                    assigned_officer_id=row["assigned_officer_id"],
                    created_at=parse_dt(row["created_at"]),
                    updated_at=parse_dt(row["updated_at"]),
                    resolved_at=parse_dt(row["resolved_at"]),
                    city=row["city"],
                    source_dataset=row["source_dataset"],
                    source_record_id=row["source_record_id"],
                    source_status=row["source_status"],
                    source_agency=row["source_agency"],
                    source_category=row["source_category"],
                    source_subcategory=row["source_subcategory"],
                    is_historical=row["is_historical"],
                    routing_status=row["routing_status"],
                    routing_reason=row["routing_reason"],
                )
            )
            imported_issues += 1

        db.flush()  # make newly added issues visible to subsequent queries

        # Import status history.
        imported_updates = 0
        skipped_updates = 0
        for row in status_updates:
            existing = db.query(models.IssueStatusUpdate).filter(models.IssueStatusUpdate.id == row["id"]).first()
            if existing is not None:
                skipped_updates += 1
                continue
            if row["issue_id"] not in live_issue_ids:
                continue
            issue = db.query(models.Issue).filter(models.Issue.id == row["issue_id"]).first()
            if issue is None:
                raise ImportSafetyError(f"Missing issue for status update {row['id']}")
            db.add(
                models.IssueStatusUpdate(
                    id=row["id"],
                    issue_id=row["issue_id"],
                    updated_by_id=row["updated_by_id"],
                    old_status=row["old_status"],
                    new_status=row["new_status"],
                    comment=row["comment"],
                    created_at=parse_dt(row["created_at"]),
                )
            )
            imported_updates += 1

        # Import audit logs.
        imported_audits = 0
        skipped_audits = 0
        for row in audit_logs:
            existing = db.query(models.AdminAuditLog).filter(models.AdminAuditLog.id == row["id"]).first()
            if existing is not None:
                skipped_audits += 1
                continue
            db.add(
                models.AdminAuditLog(
                    id=row["id"],
                    actor_user_id=row["actor_user_id"],
                    action=row["action"],
                    entity_type=row["entity_type"],
                    entity_id=row["entity_id"],
                    before_state=row["before_state"],
                    after_state=row["after_state"],
                    reason=row["reason"],
                    city=row["city"],
                    created_at=parse_dt(row["created_at"]),
                )
            )
            imported_audits += 1

        # Import image records without binary payloads (source has none).
        imported_images = 0
        skipped_images = 0
        for row in issue_images:
            existing = db.query(models.IssueImage).filter(models.IssueImage.id == row["id"]).first()
            if existing is not None:
                skipped_images += 1
                continue
            if row["issue_id"] not in live_issue_ids:
                continue
            db.add(
                models.IssueImage(
                    id=row["id"],
                    issue_id=row["issue_id"],
                    image_url=row["image_url"],
                    image_data=row.get("image_data"),
                    blur_score=row["blur_score"],
                    brightness=row["brightness"],
                    resolution_width=row["resolution_width"],
                    resolution_height=row["resolution_height"],
                    passed_verification=row["passed_verification"],
                    perceptual_hash=row["perceptual_hash"],
                    uploaded_at=parse_dt(row["uploaded_at"]),
                )
            )
            imported_images += 1

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
        engine.dispose()

    print("Import completed")
    print(f"  users imported: {imported_users}, skipped: {skipped_users}")
    print(f"  issues imported: {imported_issues}, skipped: {skipped_issues}")
    print(f"  status updates imported: {imported_updates}, skipped: {skipped_updates}")
    print(f"  audit logs imported: {imported_audits}, skipped: {skipped_audits}")
    print(f"  images imported: {imported_images}, skipped: {skipped_images}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
