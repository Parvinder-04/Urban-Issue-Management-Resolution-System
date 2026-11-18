"""Seed the public UIMRS verified starter department directory.

The 21 rows below are public agency directory information for Bengaluru and
Gurugram: agency names, categories, official public contacts, jurisdiction
notes and verification source URLs. They are **not** complaint records and
contain no personal mobile numbers.

The script is deliberately separate from application bootstrap. Bootstrap must
never create departments (see ``tests/test_bootstrap_safety.py``).

Usage (from the project root, after migrations):

    backend/venv/Scripts/python.exe scripts/seed_public_departments.py
    backend/venv/Scripts/python.exe scripts/seed_public_departments.py --dry-run
    backend/venv/Scripts/python.exe scripts/seed_public_departments.py --database-url sqlite:///...

The script is idempotent: a row is inserted only when no row with the same
city, category and agency name already exists. Existing rows are never
overwritten, so operator edits are preserved.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID, uuid5

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import models  # noqa: E402
from app.core.config import resolve_database_url  # noqa: E402
from app.db_guard import guard_startup  # noqa: E402
from app.db_migrate import HEAD_REVISION, current_revision  # noqa: E402

CITY_BENGALURU = "Bengaluru, Karnataka, India"
CITY_GURUGRAM = "Gurugram, Haryana, India"
VERIFIED_DATE = "2026-11-14"

# Stable namespace so repeated runs derive the same IDs without copying any
# private database identifiers.
DEPARTMENT_NAMESPACE = UUID("8f2a3c41-6b7d-4e59-9a10-2c3d4e5f6071")

MCG_SOURCE = "https://mcg.gov.in/contact/office-contacts"
MCG_DIRECTORY_SOURCE = "https://mcg.gov.in/contact/contact-directory"
GMDA_SOURCE = "https://services.gmda.gov.in/"
GMDA_SIGNAL_SOURCE = "https://www.gmda.gov.in/trafficlight"
POLICE_SOURCE = "https://haryanapolice.gov.in/static/TelephoneDirectory_cpggn.aspx"

MCG_JURISDICTION = (
    "Municipal Corporation of Gurugram municipal area, Gurugram district, Haryana"
)
GMDA_JURISDICTION = (
    "Gurugram Metropolitan Area as defined under the GMDA Act, 2017; asset "
    "ownership varies by road and sector"
)
POLICE_JURISDICTION = "Gurugram Police Commissionerate, Gurugram district, Haryana"


def _row(
    *,
    name: str,
    category: str,
    city: str,
    source_agency: str,
    routing_mode: str,
    short_name: str | None = None,
    jurisdiction: str | None = None,
    contact_purpose: str | None = None,
    contact_email: str | None = None,
    contact_phone: str | None = None,
    verification_source_url: str | None = None,
    verification_date: str | None = None,
) -> dict:
    return {
        "name": name,
        "handles_category": category,
        "city": city,
        "source_agency": source_agency,
        "routing_mode": routing_mode,
        "short_name": short_name,
        "jurisdiction": jurisdiction,
        "contact_purpose": contact_purpose,
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "verification_source_url": verification_source_url,
        "verification_date": verification_date,
        "is_active": True,
    }


# Bengaluru: historical agency names and categories only. No contacts were
# verified, so every row stays in manual review.
BENGALURU_ROWS = [
    _row(
        name="Bangalore Traffic Police (BTP)",
        category="TRAFFIC_MANAGEMENT",
        city=CITY_BENGALURU,
        source_agency="Bangalore Traffic Police (BTP)",
        routing_mode="manual_review",
    ),
    _row(
        name="Bangalore Water Supply and Sewerage Board (BWSSB)",
        category="DRAINAGE_SEWAGE",
        city=CITY_BENGALURU,
        source_agency="Bangalore Water Supply and Sewerage Board (BWSSB)",
        routing_mode="manual_review",
    ),
    _row(
        name="Bangalore Water Supply and Sewerage Board (BWSSB)",
        category="WATER_SUPPLY",
        city=CITY_BENGALURU,
        source_agency="Bangalore Water Supply and Sewerage Board (BWSSB)",
        routing_mode="manual_review",
    ),
    _row(
        name="Bruhat Bengaluru Mahanagara Palike (BBMP)",
        category="DRAINAGE_SEWAGE",
        city=CITY_BENGALURU,
        source_agency="Bruhat Bengaluru Mahanagara Palike (BBMP)",
        routing_mode="manual_review",
    ),
    _row(
        name="Bruhat Bengaluru Mahanagara Palike (BBMP)",
        category="GARBAGE_WASTE",
        city=CITY_BENGALURU,
        source_agency="Bruhat Bengaluru Mahanagara Palike (BBMP)",
        routing_mode="manual_review",
    ),
    _row(
        name="Bruhat Bengaluru Mahanagara Palike (BBMP)",
        category="ROAD_DAMAGE",
        city=CITY_BENGALURU,
        source_agency="Bruhat Bengaluru Mahanagara Palike (BBMP)",
        routing_mode="manual_review",
    ),
    _row(
        name="Bruhat Bengaluru Mahanagara Palike (BBMP)",
        category="STREETLIGHT",
        city=CITY_BENGALURU,
        source_agency="Bruhat Bengaluru Mahanagara Palike (BBMP)",
        routing_mode="manual_review",
    ),
    _row(
        name="Bruhat Bengaluru Mahanagara Palike (BBMP)",
        category="TRAFFIC_MANAGEMENT",
        city=CITY_BENGALURU,
        source_agency="Bruhat Bengaluru Mahanagara Palike (BBMP)",
        routing_mode="manual_review",
    ),
    _row(
        name="Bruhat Bengaluru Mahanagara Palike (BBMP)",
        category="TREES_ENVIRONMENT",
        city=CITY_BENGALURU,
        source_agency="Bruhat Bengaluru Mahanagara Palike (BBMP)",
        routing_mode="manual_review",
    ),
    _row(
        name="Bruhat Bengaluru Mahanagara Palike (BBMP)",
        category="WATER_SUPPLY",
        city=CITY_BENGALURU,
        source_agency="Bruhat Bengaluru Mahanagara Palike (BBMP)",
        routing_mode="manual_review",
    ),
]

# Gurugram: official public contacts and source URLs. Automatic routing is
# used only where the official scope is unambiguous.
GURUGRAM_ROWS = [
    _row(
        name="Municipal Corporation of Gurugram (MCG) - Solid Waste Management & Sanitation",
        category="GARBAGE_WASTE",
        city=CITY_GURUGRAM,
        source_agency="Municipal Corporation of Gurugram",
        short_name="MCG Sanitation",
        jurisdiction=MCG_JURISDICTION,
        contact_purpose=(
            "MCG 24x7 toll-free municipal complaint helpline and official public "
            "email. General complaint channel, not a zone-specific escalation contact."
        ),
        contact_email="info@mcg.gov.in",
        contact_phone="18001801817",
        verification_source_url=MCG_SOURCE,
        verification_date=VERIFIED_DATE,
        routing_mode="automatic",
    ),
    _row(
        name="Municipal Corporation of Gurugram (MCG) - Electrical & Street Lighting",
        category="STREETLIGHT",
        city=CITY_GURUGRAM,
        source_agency="Municipal Corporation of Gurugram",
        short_name="MCG Electrical",
        jurisdiction=MCG_JURISDICTION,
        contact_purpose=(
            "MCG helpline for complaints; Chief Engineer office email, under which "
            "the Electrical wing is listed in the official office directory."
        ),
        contact_email="ce2@mcg.gov.in",
        contact_phone="18001801817",
        verification_source_url=MCG_SOURCE,
        verification_date=VERIFIED_DATE,
        routing_mode="automatic",
    ),
    _row(
        name="Municipal Corporation of Gurugram (MCG) - Horticulture & Environment",
        category="TREES_ENVIRONMENT",
        city=CITY_GURUGRAM,
        source_agency="Municipal Corporation of Gurugram",
        short_name="MCG Horticulture",
        jurisdiction=MCG_JURISDICTION,
        contact_purpose=(
            "MCG helpline for complaints; Executive Engineer-VI (Horticulture & "
            "Engineering) office email."
        ),
        contact_email="ee6@mcg.gov.in",
        contact_phone="18001801817",
        verification_source_url=MCG_SOURCE,
        verification_date=VERIFIED_DATE,
        routing_mode="automatic",
    ),
    _row(
        name="Municipal Corporation of Gurugram (MCG) - Engineering & Roads",
        category="ROAD_DAMAGE",
        city=CITY_GURUGRAM,
        source_agency="Municipal Corporation of Gurugram",
        short_name="MCG Engineering",
        jurisdiction=MCG_JURISDICTION,
        contact_purpose=(
            "MCG helpline for complaints; Chief Engineer office email. Road "
            "ownership varies, so no automatic assignment is made."
        ),
        contact_email="ce2@mcg.gov.in",
        contact_phone="18001801817",
        verification_source_url=MCG_SOURCE,
        verification_date=VERIFIED_DATE,
        routing_mode="manual_review",
    ),
    _row(
        name="Gurugram Metropolitan Development Authority (GMDA) - Water Supply",
        category="WATER_SUPPLY",
        city=CITY_GURUGRAM,
        source_agency="Gurugram Metropolitan Development Authority",
        short_name="GMDA Water",
        jurisdiction=GMDA_JURISDICTION,
        contact_purpose=(
            "GMDA Complaint Handling System toll-free helpline and official email."
        ),
        contact_email="services.gmda@gmail.com",
        contact_phone="18001801817",
        verification_source_url=GMDA_SOURCE,
        verification_date=VERIFIED_DATE,
        routing_mode="manual_review",
    ),
    _row(
        name="Municipal Corporation of Gurugram (MCG) - Drains & Sewerage",
        category="DRAINAGE_SEWAGE",
        city=CITY_GURUGRAM,
        source_agency="Municipal Corporation of Gurugram",
        short_name="MCG Drains",
        jurisdiction=MCG_JURISDICTION,
        contact_purpose=(
            "MCG helpline for complaints; Chief Engineer office email. GMDA also "
            "publishes new sewerage/drainage connections, so ownership varies."
        ),
        contact_email="ce2@mcg.gov.in",
        contact_phone="18001801817",
        verification_source_url=MCG_SOURCE,
        verification_date=VERIFIED_DATE,
        routing_mode="manual_review",
    ),
    _row(
        name="Gurugram Traffic Police - Traffic Management",
        category="TRAFFIC_MANAGEMENT",
        city=CITY_GURUGRAM,
        source_agency="Gurugram Traffic Police",
        short_name="Gurugram Traffic Police",
        jurisdiction=POLICE_JURISDICTION,
        contact_purpose=(
            "DCP Traffic, Gurugram office line and official email for "
            "traffic-management complaints."
        ),
        contact_email="dcp.trafficggn@hry.nic.in",
        contact_phone="0124-2576015",
        verification_source_url=POLICE_SOURCE,
        verification_date=VERIFIED_DATE,
        routing_mode="automatic",
    ),
    _row(
        name="Gurugram Metropolitan Development Authority (GMDA) - Traffic Signals",
        category="TRAFFIC_SIGNAL",
        city=CITY_GURUGRAM,
        source_agency="Gurugram Metropolitan Development Authority",
        short_name="GMDA Traffic Signals",
        jurisdiction=(
            "Gurugram Metropolitan Area; traffic-signal ownership varies by road, "
            "so no automatic assignment is made"
        ),
        contact_purpose=(
            "GMDA Complaint Handling System toll-free helpline and official email; "
            "GMDA publishes a dedicated traffic-light complaint channel."
        ),
        contact_email="services.gmda@gmail.com",
        contact_phone="18001801817",
        verification_source_url=GMDA_SIGNAL_SOURCE,
        verification_date=VERIFIED_DATE,
        routing_mode="manual_review",
    ),
    _row(
        name="Municipal Corporation of Gurugram (MCG) - Public Property & Assets",
        category="PUBLIC_PROPERTY_DAMAGE",
        city=CITY_GURUGRAM,
        source_agency="Municipal Corporation of Gurugram",
        short_name="MCG Assets",
        jurisdiction=MCG_JURISDICTION,
        contact_purpose=(
            "MCG helpline and official public email. Asset ownership is not "
            "verified per location, so no automatic assignment is made."
        ),
        contact_email="info@mcg.gov.in",
        contact_phone="18001801817",
        verification_source_url=MCG_DIRECTORY_SOURCE,
        verification_date=VERIFIED_DATE,
        routing_mode="manual_review",
    ),
    _row(
        name="Municipal Corporation of Gurugram (MCG) - Town Planning & Building Enforcement",
        category="ILLEGAL_CONSTRUCTION",
        city=CITY_GURUGRAM,
        source_agency="Municipal Corporation of Gurugram",
        short_name="MCG Town Planning",
        jurisdiction=MCG_JURISDICTION,
        contact_purpose=(
            "Chief Town Planner office line and official email for planning and "
            "building matters. GMDA also approves building plans in its area."
        ),
        contact_email="ctp@mcg.gov.in",
        contact_phone="0124-2371977",
        verification_source_url=MCG_SOURCE,
        verification_date=VERIFIED_DATE,
        routing_mode="manual_review",
    ),
    _row(
        name="Municipal Corporation of Gurugram (MCG) - Citizen Grievance Redressal",
        category="OTHER",
        city=CITY_GURUGRAM,
        source_agency="Municipal Corporation of Gurugram",
        short_name="MCG Grievance",
        jurisdiction=MCG_JURISDICTION,
        contact_purpose=(
            "MCG 24x7 toll-free helpline and official public email for general "
            "grievances. Disabled for automatic routing because 'Other' has no "
            "verified responsible authority."
        ),
        contact_email="info@mcg.gov.in",
        contact_phone="18001801817",
        verification_source_url=MCG_DIRECTORY_SOURCE,
        verification_date=VERIFIED_DATE,
        routing_mode="disabled",
    ),
]

DEPARTMENT_ROWS = BENGALURU_ROWS + GURUGRAM_ROWS


def department_id(row: dict) -> str:
    key = f"{row['city']}|{row['handles_category']}|{row['name']}"
    return str(uuid5(DEPARTMENT_NAMESPACE, key))


def _make_engine(database_url: str):
    kwargs = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(database_url, **kwargs)


def seed(database_url: str, dry_run: bool = False) -> dict:
    engine = _make_engine(database_url)
    try:
        state = guard_startup(engine, database_url)
        if state != "validated":
            raise RuntimeError(
                f"Database state is {state!r}; apply the Alembic migrations first."
            )
        revision = current_revision(database_url)
        if revision != HEAD_REVISION:
            raise RuntimeError(
                f"Database revision {revision!r} does not match {HEAD_REVISION!r}."
            )

        session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        db = session_factory()
        inserted = 0
        skipped = 0
        try:
            for row in DEPARTMENT_ROWS:
                category = models.IssueCategory[row["handles_category"]]
                exists = (
                    db.query(models.Department)
                    .filter(
                        models.Department.city == row["city"],
                        models.Department.handles_category == category,
                        models.Department.name == row["name"],
                    )
                    .first()
                )
                if exists is not None:
                    skipped += 1
                    continue
                if dry_run:
                    inserted += 1
                    continue
                db.add(
                    models.Department(
                        id=department_id(row),
                        name=row["name"],
                        handles_category=category,
                        city=row["city"],
                        source_agency=row["source_agency"],
                        short_name=row["short_name"],
                        jurisdiction=row["jurisdiction"],
                        contact_purpose=row["contact_purpose"],
                        contact_email=row["contact_email"],
                        contact_phone=row["contact_phone"],
                        verification_source_url=row["verification_source_url"],
                        verification_date=row["verification_date"],
                        routing_mode=row["routing_mode"],
                        is_active=row["is_active"],
                    )
                )
                inserted += 1
            if dry_run:
                db.rollback()
            else:
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return {
            "database_url": database_url,
            "revision": revision,
            "total_rows": len(DEPARTMENT_ROWS),
            "inserted": inserted,
            "skipped_existing": skipped,
            "dry_run": dry_run,
        }
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed the verified public starter department directory."
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy URL. Defaults to the application DATABASE_URL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be inserted without changing the database.",
    )
    args = parser.parse_args(argv)

    database_url = args.database_url or resolve_database_url()
    try:
        summary = seed(database_url, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 - operator-facing message
        print(f"Department seed failed: {exc}", file=sys.stderr)
        return 1

    print(f"database:        {summary['database_url']}")
    print(f"revision:        {summary['revision']}")
    print(f"directory rows:  {summary['total_rows']}")
    print(f"inserted:        {summary['inserted']}")
    print(f"already present: {summary['skipped_existing']}")
    if summary["dry_run"]:
        print("dry run: no rows were written")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
