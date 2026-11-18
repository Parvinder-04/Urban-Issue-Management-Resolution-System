"""
Database models.

Uses GeoAlchemy2's Geography(Point) type for issue locations — this is what
lets us run real PostGIS spatial queries (ST_DWithin for "issues within X
metres", ST_ClusterDBSCAN for server-side clustering as a fallback / cross-
check against the Python-side DBSCAN in geo_clustering_service.py) directly
in SQL when the dataset grows too large to cluster in-memory.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Text, Float, Integer, Boolean, DateTime, ForeignKey, Enum, JSON,
    event,
)
from sqlalchemy.orm import relationship

from app.database import Base


def gen_uuid():
    return str(uuid.uuid4())


class UserRole(str, enum.Enum):
    CITIZEN = "citizen"
    OFFICER = "officer"
    ADMIN = "admin"


class IssueCategory(str, enum.Enum):
    ROAD_DAMAGE = "Road_Damage"
    GARBAGE_WASTE = "Garbage_Waste"
    STREETLIGHT = "Streetlight"
    WATER_SUPPLY = "Water_Supply"
    DRAINAGE_SEWAGE = "Drainage_Sewage"
    TRAFFIC_SIGNAL = "Traffic_Signal"
    TRAFFIC_MANAGEMENT = "Traffic_Management"
    TREES_ENVIRONMENT = "Trees_Environment"
    ILLEGAL_CONSTRUCTION = "Illegal_Construction"
    PUBLIC_PROPERTY_DAMAGE = "Public_Property_Damage"
    OTHER = "Other"


class IssueStatus(str, enum.Enum):
    REPORTED = "reported"
    ACKNOWLEDGED = "acknowledged"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"


class PriorityLevel(str, enum.Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"


class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    name = Column(String(120), nullable=False)
    email = Column(String(150), unique=True, nullable=False, index=True)
    phone = Column(String(20), unique=True, nullable=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(Enum(UserRole), default=UserRole.CITIZEN, nullable=False)
    department_id = Column(String(36), ForeignKey("departments.id"), nullable=True)
    preferred_language = Column(String(10), default="en")
    created_at = Column(DateTime, default=datetime.utcnow)

    department = relationship("Department", back_populates="officers")
    issues_reported = relationship("Issue", back_populates="reporter",
                                    foreign_keys="Issue.reporter_id")


class Department(Base):
    __tablename__ = "departments"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    name = Column(String(120), nullable=False)
    handles_category = Column(Enum(IssueCategory), nullable=False)
    contact_email = Column(String(150))
    contact_phone = Column(String(20))

    city = Column(String(120), nullable=True)
    source_agency = Column(String(150), nullable=True)

    # --- Verified directory metadata (nullable: historical rows predate it) ---
    short_name = Column(String(60), nullable=True)
    jurisdiction = Column(String(300), nullable=True)
    contact_purpose = Column(String(200), nullable=True)
    verification_source_url = Column(String(500), nullable=True)
    # ISO-8601 date (YYYY-MM-DD) on which the official source was checked.
    verification_date = Column(String(10), nullable=True)
    # automatic | manual_review | disabled. Existing rows default to the safest
    # value so an unverified mapping can never be auto-assigned by accident.
    routing_mode = Column(
        String(20), nullable=False, default="manual_review",
        server_default="manual_review",
    )
    is_active = Column(Boolean, nullable=False, default=True, server_default="1")

    officers = relationship("User", back_populates="department")
    issues = relationship("Issue", back_populates="department")


class Issue(Base):
    __tablename__ = "issues"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    ticket_number = Column(String(30), unique=True, nullable=False, index=True)  # e.g. UIMRS-2026-00123

    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=False)

    # --- AI classification output ---
    category = Column(Enum(IssueCategory), nullable=False)
    # Uncalibrated model score (ML) or heuristic score (rules); never a calibrated probability.
    category_confidence = Column(Float, default=0.0)
    # bengaluru_v2_2_ml | india_keyword_rule | needs_clarification | empty_input
    category_source = Column(String(20))

    # --- AI classification explainability (nullable: historical rows have none) ---
    # Model/version that produced the label, e.g. india_bengaluru_v2_2_candidate.
    category_model_version = Column(String(80), nullable=True)
    # Whether the classifier asked for human review (True/False), or unknown (NULL).
    category_needs_review = Column(Boolean, nullable=True)
    # The classifier's actual reason when review was required.
    category_review_reason = Column(Text, nullable=True)
    # Alternatives actually produced by the model: [{"category": ..., "model_score": ...}].
    category_top_alternatives = Column(JSON, nullable=True)
    # uncalibrated_model_score | rule_heuristic | none
    category_score_kind = Column(String(40), nullable=True)

    # --- AI priority scoring output ---
    priority_score = Column(Float, default=0.0)
    priority_level = Column(Enum(PriorityLevel), default=PriorityLevel.LOW)
    # Reasons produced by the real PriorityEngine, stored verbatim.
    priority_reasons = Column(JSON, nullable=True)

    status = Column(Enum(IssueStatus), default=IssueStatus.REPORTED, nullable=False)

    # --- Location (PostGIS) ---
    location = Column(String(100), nullable=True)
    latitude = Column(Float, nullable=False)   # denormalized for easy API responses
    longitude = Column(Float, nullable=False)
    address_text = Column(String(300))

    # --- Duplicate / clustering ---
    duplicate_of_issue_id = Column(String(36), ForeignKey("issues.id"), nullable=True)
    duplicate_count = Column(Integer, default=0)
    hotspot_cluster_id = Column(Integer, nullable=True)

    reporter_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    department_id = Column(String(36), ForeignKey("departments.id"), nullable=True)
    assigned_officer_id = Column(String(36), ForeignKey("users.id"), nullable=True)

    # --- Routing outcome ---
    # assigned | unassigned_review | unassigned_no_mapping.
    # NULL on historical rows: they are never re-routed or re-assigned.
    routing_status = Column(String(30), nullable=True)
    # Truthful, human-readable explanation of the routing decision.
    routing_reason = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)

    # Source provenance for imported historical records.
    city = Column(String(120), nullable=True)
    source_dataset = Column(String(200), nullable=True)
    source_record_id = Column(String(80), nullable=True, index=True)
    source_status = Column(String(40), nullable=True)
    source_agency = Column(String(150), nullable=True)
    source_category = Column(String(150), nullable=True)
    source_subcategory = Column(String(200), nullable=True)
    is_historical = Column(Boolean, nullable=False, default=False)

    reporter = relationship("User", back_populates="issues_reported", foreign_keys=[reporter_id])
    department = relationship("Department", back_populates="issues")
    # Read-only convenience relationship for administrative oversight. Ownership
    # is still changed exclusively through the officer claim workflow.
    assigned_officer = relationship("User", foreign_keys=[assigned_officer_id])
    images = relationship("IssueImage", back_populates="issue", cascade="all, delete-orphan")
    status_updates = relationship("IssueStatusUpdate", back_populates="issue",
                                   cascade="all, delete-orphan")


class IssueImage(Base):
    __tablename__ = "issue_images"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    issue_id = Column(String(36), ForeignKey("issues.id"), nullable=False)
    image_url = Column(String(500), nullable=False)

    # --- CV verification output ---
    blur_score = Column(Float)
    brightness = Column(Float)
    resolution_width = Column(Integer)
    resolution_height = Column(Integer)
    passed_verification = Column(Boolean, default=False)
    perceptual_hash = Column(String(80))

    uploaded_at = Column(DateTime, default=datetime.utcnow)

    issue = relationship("Issue", back_populates="images")


class IssueStatusUpdate(Base):
    """Audit trail: every status change + who made it + optional comment."""
    __tablename__ = "issue_status_updates"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    issue_id = Column(String(36), ForeignKey("issues.id"), nullable=False)
    updated_by_id = Column(String(36), ForeignKey("users.id"), nullable=True)

    old_status = Column(Enum(IssueStatus), nullable=True)
    new_status = Column(Enum(IssueStatus), nullable=False)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    issue = relationship("Issue", back_populates="status_updates")


class AdminAuditLog(Base):
    """Immutable administrative audit trail.

    This is intentionally separate from :class:`IssueStatusUpdate`: the status
    table records complaint workflow transitions, while this table records
    administrative decisions (jurisdiction routing, leave-unassigned decisions,
    officer provisioning and department changes).

    Payloads contain identifiers and state snapshots only. Passwords, hashes,
    bearer tokens and citizen contact details are never written here.
    """

    __tablename__ = "admin_audit_logs"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    actor_user_id = Column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    action = Column(String(64), nullable=False, index=True)
    entity_type = Column(String(40), nullable=False, index=True)
    entity_id = Column(String(36), nullable=True, index=True)
    before_state = Column(JSON, nullable=True)
    after_state = Column(JSON, nullable=True)
    reason = Column(Text, nullable=True)
    # Canonical city the decision applies to, where one exists. Nullable because
    # not every administrative action is city-scoped.
    city = Column(String(120), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    actor = relationship("User", foreign_keys=[actor_user_id])


def _reject_admin_audit_mutation(mapper, connection, target):  # pragma: no cover - defensive
    """Defensive guard: audit rows are append-only through the ORM.

    Bulk maintenance operations (``Query.delete()``) and database-level tools
    bypass ORM events, but normal application code can never rewrite history.
    No API exposes an update or delete operation.
    """
    raise ValueError("Administrative audit log entries are immutable.")


event.listen(AdminAuditLog, "before_update", _reject_admin_audit_mutation)
event.listen(AdminAuditLog, "before_delete", _reject_admin_audit_mutation)
