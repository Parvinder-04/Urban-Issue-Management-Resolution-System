"""Administrator-portal business rules.

The functions here are shared by the admin router and the unit tests. Every
operational query excludes historical records unless the caller explicitly asks
for the historical archive. Routing decisions are validated against the real
verified directory: city, category, active status, routing mode and verification
provenance must all match before a complaint is assigned.

No function invents a department, contact, officer or metric.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import func, or_
from sqlalchemy.orm import Query, Session

from app import models, schemas
from app.services.admin_audit import (
    ACTION_ISSUE_LEFT_UNASSIGNED,
    ACTION_ISSUE_ROUTED,
    ENTITY_ISSUE,
    issue_routing_state,
    record_admin_audit,
)
from app.services.department_routing import (
    ROUTING_MODE_AUTOMATIC,
    ROUTING_MODE_DISABLED,
    ROUTING_MODE_MANUAL_REVIEW,
    ROUTING_STATUS_ASSIGNED,
    ROUTING_STATUS_UNASSIGNED_NO_MAPPING,
    ROUTING_STATUS_UNASSIGNED_REVIEW,
)
from app.services.officer_workflow import (
    ACTIVE_ISSUE_STATUSES,
    SUPPORTED_OFFICER_CITIES,
)

IssueScope = Literal["live", "historical", "all"]


class AdminOperationError(Exception):
    """A controlled administrative refusal mapped to an HTTP status."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------
def _category_value(category) -> str | None:
    if category is None:
        return None
    return category.value if isinstance(category, models.IssueCategory) else str(category)


def _priority_value(priority) -> str:
    if priority is None:
        return "Unscored"
    return (
        priority.value
        if isinstance(priority, models.PriorityLevel)
        else str(priority)
    )


def department_out(department: models.Department) -> schemas.AdminDepartmentOut:
    return schemas.AdminDepartmentOut(
        id=department.id,
        name=department.name,
        short_name=department.short_name,
        source_agency=department.source_agency,
        handles_category=_category_value(department.handles_category),
        city=department.city,
        jurisdiction=department.jurisdiction,
        contact_email=department.contact_email,
        contact_phone=department.contact_phone,
        contact_purpose=department.contact_purpose,
        verification_source_url=department.verification_source_url,
        verification_date=department.verification_date,
        routing_mode=department.routing_mode,
        is_active=bool(department.is_active),
    )


def admin_issue_out(
    issue: models.Issue, *, include_reporter: bool = False
) -> schemas.AdminIssueOut:
    row = schemas.AdminIssueOut.model_validate(issue)
    row.department_name = issue.department.name if issue.department else None
    row.assigned_officer_name = (
        issue.assigned_officer.name if issue.assigned_officer else None
    )
    if include_reporter:
        row.reporter_name = issue.reporter.name if issue.reporter else None
    return row


def officer_out(user: models.User) -> schemas.AdminOfficerOut:
    department = user.department
    return schemas.AdminOfficerOut(
        id=user.id,
        name=user.name,
        email=user.email,
        phone=user.phone,
        role=user.role,
        department_id=user.department_id,
        department_name=department.name if department else None,
        department_city=department.city if department else None,
        preferred_language=user.preferred_language or "en",
        created_at=user.created_at,
    )


def _candidate(
    issue: models.Issue, department: models.Department
) -> schemas.AdminRoutingCandidateOut:
    reasons: list[str] = []
    if not department.is_active:
        reasons.append("Department is inactive.")
    if department.city != issue.city:
        reasons.append("Department city does not match the complaint city.")
    if department.handles_category != issue.category:
        reasons.append("Department category does not match the complaint category.")
    mode = department.routing_mode or ROUTING_MODE_MANUAL_REVIEW
    if mode == ROUTING_MODE_DISABLED:
        reasons.append("This directory mapping is disabled for routing.")
    elif mode not in (ROUTING_MODE_AUTOMATIC, ROUTING_MODE_MANUAL_REVIEW):
        reasons.append("This directory mapping has no approved routing mode.")
    if not department.verification_source_url:
        reasons.append("No official verification source is recorded.")
    if not department.verification_date:
        reasons.append("No official verification date is recorded.")

    base = department_out(department)
    return schemas.AdminRoutingCandidateOut(
        **base.model_dump(),
        selectable=not reasons,
        blocked_reasons=reasons,
    )


def list_candidates(
    db: Session, issue: models.Issue
) -> list[schemas.AdminRoutingCandidateOut]:
    """Return every same-city/same-category directory row, with eligibility."""
    if not issue.city or issue.category is None:
        return []
    rows = (
        db.query(models.Department)
        .filter(
            models.Department.city == issue.city,
            models.Department.handles_category == issue.category,
        )
        .order_by(models.Department.name.asc())
        .all()
    )
    return [_candidate(issue, row) for row in rows]


# ---------------------------------------------------------------------------
# Review queue and issue detail
# ---------------------------------------------------------------------------
def review_queue_query(db: Session) -> Query:
    """Live complaints that genuinely need a jurisdiction decision.

    Historical rows, already-routed complaints, complaints owned by an officer
    and closed complaints are excluded. ``unassigned_no_mapping`` rows are
    eligible for investigation because a real verified mapping may still exist.
    """
    return db.query(models.Issue).filter(
        models.Issue.is_historical.is_(False),
        models.Issue.department_id.is_(None),
        models.Issue.assigned_officer_id.is_(None),
        models.Issue.status.in_(ACTIVE_ISSUE_STATUSES),
        models.Issue.routing_status.in_(
            (
                ROUTING_STATUS_UNASSIGNED_REVIEW,
                ROUTING_STATUS_UNASSIGNED_NO_MAPPING,
            )
        ),
    )


def list_review_queue(
    db: Session,
    *,
    city: str | None = None,
    category: models.IssueCategory | None = None,
    priority: models.PriorityLevel | None = None,
    skip: int = 0,
    limit: int = 50,
) -> list[schemas.AdminIssueOut]:
    query = review_queue_query(db)
    if city:
        query = query.filter(models.Issue.city == city)
    if category:
        query = query.filter(models.Issue.category == category)
    if priority:
        query = query.filter(models.Issue.priority_level == priority)
    rows = (
        query.order_by(
            models.Issue.priority_score.desc(),
            models.Issue.created_at.asc(),
        )
        .offset(max(skip, 0))
        .limit(min(max(limit, 1), 200))
        .all()
    )
    return [admin_issue_out(row) for row in rows]


def routing_block_reason(issue: models.Issue) -> str | None:
    """Explain why an issue cannot receive a routing decision, or ``None``."""
    if issue.is_historical:
        return "Historical complaint records are immutable and cannot be routed."
    if issue.status not in ACTIVE_ISSUE_STATUSES:
        return "Closed complaints cannot be routed."
    if issue.department_id is not None:
        return "Complaint is already routed to a department."
    if issue.assigned_officer_id is not None:
        return "Complaint is already assigned to an officer."
    if issue.routing_status not in (
        ROUTING_STATUS_UNASSIGNED_REVIEW,
        ROUTING_STATUS_UNASSIGNED_NO_MAPPING,
    ):
        return "Complaint is not awaiting jurisdiction review."
    return None


def get_admin_issue_detail(
    db: Session, issue_id: str
) -> schemas.AdminIssueDetailOut:
    issue = db.query(models.Issue).filter(models.Issue.id == issue_id).first()
    if issue is None:
        raise AdminOperationError("Issue not found.", 404)

    images = (
        db.query(models.IssueImage)
        .filter(models.IssueImage.issue_id == issue.id)
        .order_by(models.IssueImage.uploaded_at.asc())
        .all()
    )
    history = (
        db.query(models.IssueStatusUpdate)
        .filter(models.IssueStatusUpdate.issue_id == issue.id)
        .order_by(models.IssueStatusUpdate.created_at.asc())
        .all()
    )
    block_reason = routing_block_reason(issue)

    return schemas.AdminIssueDetailOut(
        issue=admin_issue_out(issue, include_reporter=True),
        department=department_out(issue.department) if issue.department else None,
        reporter_name=issue.reporter.name if issue.reporter else None,
        assigned_officer=(
            schemas.AdminOfficerSummaryOut(
                id=issue.assigned_officer.id,
                name=issue.assigned_officer.name,
                role=issue.assigned_officer.role,
                department_id=issue.assigned_officer.department_id,
            )
            if issue.assigned_officer
            else None
        ),
        images=[schemas.IssueImageOut.model_validate(image) for image in images],
        history=[
            schemas.IssueStatusUpdateOut.model_validate(entry) for entry in history
        ],
        candidates=list_candidates(db, issue),
        can_route=block_reason is None,
        routing_block_reason=block_reason,
    )


# ---------------------------------------------------------------------------
# Routing decisions
# ---------------------------------------------------------------------------
def _clean_reason(reason: str) -> str:
    cleaned = (reason or "").strip()
    if len(cleaned) < 10:
        raise AdminOperationError(
            "A meaningful administrative justification of at least 10 "
            "characters is required.",
            422,
        )
    if len(cleaned) > 2000:
        raise AdminOperationError(
            "The administrative justification must be 2000 characters or fewer.",
            422,
        )
    return cleaned


def _reviewable_issue(db: Session, issue_id: str) -> models.Issue:
    issue = db.query(models.Issue).filter(models.Issue.id == issue_id).first()
    if issue is None:
        raise AdminOperationError("Issue not found.", 404)
    block_reason = routing_block_reason(issue)
    if block_reason is not None:
        status_code = 400 if issue.is_historical else 409
        raise AdminOperationError(block_reason, status_code)
    return issue


def _validated_department(
    db: Session, issue: models.Issue, department_id: str
) -> models.Department:
    department = (
        db.query(models.Department)
        .filter(models.Department.id == department_id)
        .first()
    )
    if department is None:
        raise AdminOperationError(
            "Selected department does not exist. No mapping was created.", 404
        )

    reasons: list[str] = []
    if not department.is_active:
        reasons.append("Selected department is inactive.")
    if not issue.city or department.city != issue.city:
        reasons.append("Selected department is not in the complaint's city.")
    if department.handles_category != issue.category:
        reasons.append("Selected department does not handle this category.")
    mode = department.routing_mode or ROUTING_MODE_MANUAL_REVIEW
    if mode == ROUTING_MODE_DISABLED:
        reasons.append("This directory mapping is disabled for routing.")
    elif mode not in (ROUTING_MODE_AUTOMATIC, ROUTING_MODE_MANUAL_REVIEW):
        reasons.append("This directory mapping has no approved routing mode.")
    if not department.verification_source_url:
        reasons.append("Selected department has no official verification source.")
    if not department.verification_date:
        reasons.append("Selected department has no official verification date.")
    if reasons:
        raise AdminOperationError(" ".join(reasons), 400)
    return department


def route_issue(
    db: Session,
    *,
    actor: models.User,
    issue_id: str,
    department_id: str,
    reason: str,
) -> tuple[models.Issue, models.AdminAuditLog]:
    """Assign a live review complaint to a verified, matching department.

    The department id is the only directory input. City, category, active state,
    routing mode and verification provenance are all re-read from the database;
    the client cannot widen them. Officer ownership is never changed here.
    """
    issue = _reviewable_issue(db, issue_id)
    cleaned_reason = _clean_reason(reason)
    department = _validated_department(db, issue, department_id)

    before = issue_routing_state(issue)
    issue.department_id = department.id
    issue.routing_status = ROUTING_STATUS_ASSIGNED
    issue.routing_reason = (
        f"Administrator decision: routed to '{department.name}' "
        f"({department.city}) for category '{_category_value(issue.category)}'. "
        f"Justification: {cleaned_reason}"
    )
    issue.updated_at = datetime.now(timezone.utc)
    after = issue_routing_state(issue)

    audit = record_admin_audit(
        db,
        actor=actor,
        action=ACTION_ISSUE_ROUTED,
        entity_type=ENTITY_ISSUE,
        entity_id=issue.id,
        before_state=before,
        after_state=after,
        reason=cleaned_reason,
        city=issue.city,
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(issue)
    return issue, audit


def leave_issue_unassigned(
    db: Session,
    *,
    actor: models.User,
    issue_id: str,
    reason: str,
) -> tuple[models.Issue, models.AdminAuditLog]:
    """Keep a live review complaint unassigned with a documented reason."""
    issue = _reviewable_issue(db, issue_id)
    cleaned_reason = _clean_reason(reason)

    before = issue_routing_state(issue)
    issue.department_id = None
    issue.routing_status = ROUTING_STATUS_UNASSIGNED_REVIEW
    issue.routing_reason = (
        "Administrator kept this complaint unassigned after jurisdiction "
        f"review. Reason: {cleaned_reason}"
    )
    issue.updated_at = datetime.now(timezone.utc)
    after = issue_routing_state(issue)

    audit = record_admin_audit(
        db,
        actor=actor,
        action=ACTION_ISSUE_LEFT_UNASSIGNED,
        entity_type=ENTITY_ISSUE,
        entity_id=issue.id,
        before_state=before,
        after_state=after,
        reason=cleaned_reason,
        city=issue.city,
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(issue)
    return issue, audit


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------
def _apply_scope_filters(
    query: Query,
    *,
    city: str | None,
    department_id: str | None,
    start=None,
    end=None,
) -> Query:
    if city:
        query = query.filter(models.Issue.city == city)
    if department_id:
        query = query.filter(models.Issue.department_id == department_id)
    if start is not None:
        query = query.filter(models.Issue.created_at >= start)
    if end is not None:
        query = query.filter(models.Issue.created_at <= end)
    return query


def build_overview(
    db: Session,
    *,
    city: str | None = None,
    department_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> schemas.AdminOverviewOut:
    """Build operational and historical sections from separate queries.

    Historical Bengaluru rows never enter the live section, and live Gurugram
    counts are never presented as historical results.

    ``date_from``/``date_to`` are optional ISO-8601 filters on ``created_at``.
    They are applied consistently to every section so a filtered overview can
    never mix two different time windows.
    """
    start = parse_audit_datetime(date_from)
    end = parse_audit_datetime(date_to, end_of_day=True)
    if start is not None and end is not None and start > end:
        raise AdminOperationError("date_from must not be later than date_to.", 422)

    live = _apply_scope_filters(
        db.query(models.Issue).filter(models.Issue.is_historical.is_(False)),
        city=city,
        department_id=department_id,
        start=start,
        end=end,
    )
    historical = _apply_scope_filters(
        db.query(models.Issue).filter(models.Issue.is_historical.is_(True)),
        city=city,
        department_id=department_id,
        start=start,
        end=end,
    )

    total_live = live.count()
    status_rows = (
        live.with_entities(models.Issue.status, func.count(models.Issue.id))
        .group_by(models.Issue.status)
        .all()
    )
    status_counts = {status: count for status, count in status_rows}
    statuses = schemas.AdminLiveStatusCounts(
        reported=int(status_counts.get(models.IssueStatus.REPORTED, 0)),
        acknowledged=int(status_counts.get(models.IssueStatus.ACKNOWLEDGED, 0)),
        in_progress=int(status_counts.get(models.IssueStatus.IN_PROGRESS, 0)),
        resolved=int(status_counts.get(models.IssueStatus.RESOLVED, 0)),
        rejected=int(status_counts.get(models.IssueStatus.REJECTED, 0)),
        duplicate=int(status_counts.get(models.IssueStatus.DUPLICATE, 0)),
    )

    assigned = live.filter(models.Issue.department_id.isnot(None)).count()
    unassigned = live.filter(models.Issue.department_id.is_(None)).count()
    awaiting_review = live.filter(
        models.Issue.department_id.is_(None),
        models.Issue.routing_status == ROUTING_STATUS_UNASSIGNED_REVIEW,
    ).count()
    needs_ai_review = live.filter(
        models.Issue.category_needs_review.is_(True)
    ).count()

    workload_rows = (
        db.query(
            models.Department.id,
            models.Department.name,
            models.Department.city,
            models.Department.handles_category,
            func.count(models.Issue.id),
        )
        .join(models.Issue, models.Issue.department_id == models.Department.id)
        .filter(models.Issue.is_historical.is_(False))
        .filter(models.Issue.department_id.isnot(None))
    )
    if city:
        workload_rows = workload_rows.filter(models.Issue.city == city)
    if department_id:
        workload_rows = workload_rows.filter(
            models.Department.id == department_id
        )
    if start is not None:
        workload_rows = workload_rows.filter(models.Issue.created_at >= start)
    if end is not None:
        workload_rows = workload_rows.filter(models.Issue.created_at <= end)
    workload_rows = (
        workload_rows.group_by(
            models.Department.id,
            models.Department.name,
            models.Department.city,
            models.Department.handles_category,
        )
        .order_by(func.count(models.Issue.id).desc())
        .all()
    )
    department_workload = [
        schemas.AdminDepartmentWorkloadOut(
            department_id=row[0],
            name=row[1],
            city=row[2],
            handles_category=_category_value(row[3]),
            count=int(row[4]),
        )
        for row in workload_rows
    ]

    city_rows = (
        live.with_entities(models.Issue.city, func.count(models.Issue.id))
        .group_by(models.Issue.city)
        .order_by(func.count(models.Issue.id).desc())
        .all()
    )
    city_distribution = [
        schemas.AdminCityCountOut(city=row[0], count=int(row[1]))
        for row in city_rows
    ]

    live_category_rows = (
        live.with_entities(models.Issue.category, func.count(models.Issue.id))
        .group_by(models.Issue.category)
        .order_by(func.count(models.Issue.id).desc())
        .all()
    )
    live_category_distribution = [
        {"category": _category_value(row[0]), "count": int(row[1])}
        for row in live_category_rows
    ]

    live_priority_rows = (
        live.with_entities(
            models.Issue.priority_level, func.count(models.Issue.id)
        )
        .group_by(models.Issue.priority_level)
        .order_by(func.count(models.Issue.id).desc())
        .all()
    )
    live_priority_distribution = [
        {"priority": _priority_value(row[0]), "count": int(row[1])}
        for row in live_priority_rows
    ]

    total_historical = historical.count()
    agency_rows = (
        historical.with_entities(
            models.Issue.source_agency, func.count(models.Issue.id)
        )
        .group_by(models.Issue.source_agency)
        .order_by(func.count(models.Issue.id).desc())
        .all()
    )
    historical_category_rows = (
        historical.with_entities(
            models.Issue.category, func.count(models.Issue.id)
        )
        .group_by(models.Issue.category)
        .order_by(func.count(models.Issue.id).desc())
        .all()
    )

    return schemas.AdminOverviewOut(
        filters=schemas.AdminOverviewFiltersOut(
            city=city,
            department_id=department_id,
            date_from=date_from,
            date_to=date_to,
        ),
        live=schemas.AdminLiveOverviewOut(
            total=total_live,
            statuses=statuses,
            assigned=assigned,
            unassigned=unassigned,
            awaiting_review=awaiting_review,
            needs_ai_review=needs_ai_review,
            department_workload=department_workload,
            city_distribution=city_distribution,
            category_distribution=live_category_distribution,
            priority_distribution=live_priority_distribution,
        ),
        historical=schemas.AdminHistoricalOverviewOut(
            total=total_historical,
            source_agency_distribution=[
                {"source_agency": row[0], "count": int(row[1])}
                for row in agency_rows
            ],
            category_distribution=[
                {"category": _category_value(row[0]), "count": int(row[1])}
                for row in historical_category_rows
            ],
            note=(
                "Historical source records are shown separately from live "
                "operations. They are never added to live complaint counts and "
                "are never routed, claimed or updated."
            ),
        ),
        resolution_timing_available=False,
        notes=[
            "Live counts include only records with is_historical = false.",
            "Historical counts include only imported source records.",
            "Resolution-time averages are not calculated because a complete, "
            "comparable live resolution timestamp history is not available.",
        ],
    )


# ---------------------------------------------------------------------------
# Live complaint listing
# ---------------------------------------------------------------------------
def list_admin_issues(
    db: Session,
    *,
    scope: IssueScope = "live",
    status: models.IssueStatus | None = None,
    category: models.IssueCategory | None = None,
    priority: models.PriorityLevel | None = None,
    city: str | None = None,
    department_id: str | None = None,
    routing_status: str | None = None,
    q: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> list[schemas.AdminIssueOut]:
    query = db.query(models.Issue)
    if scope == "live":
        query = query.filter(models.Issue.is_historical.is_(False))
    elif scope == "historical":
        query = query.filter(models.Issue.is_historical.is_(True))

    if status:
        query = query.filter(models.Issue.status == status)
    if category:
        query = query.filter(models.Issue.category == category)
    if priority:
        query = query.filter(models.Issue.priority_level == priority)
    if city:
        query = query.filter(models.Issue.city == city)
    if department_id:
        query = query.filter(models.Issue.department_id == department_id)
    if routing_status:
        query = query.filter(models.Issue.routing_status == routing_status)

    search = (q or "").strip()
    if search:
        if len(search) > 120:
            raise AdminOperationError(
                "Search text must be 120 characters or fewer.", 422
            )
        pattern = f"%{search}%"
        query = query.filter(
            or_(
                models.Issue.ticket_number.ilike(pattern),
                models.Issue.title.ilike(pattern),
                models.Issue.description.ilike(pattern),
            )
        )

    if scope == "historical":
        ordered = query.order_by(models.Issue.created_at.desc())
    else:
        ordered = query.order_by(
            models.Issue.priority_score.desc(),
            models.Issue.created_at.desc(),
        )

    rows = (
        ordered.offset(max(skip, 0))
        .limit(min(max(limit, 1), 200))
        .all()
    )
    return [admin_issue_out(row) for row in rows]


# ---------------------------------------------------------------------------
# Officer management and directory
# ---------------------------------------------------------------------------
def list_officers(
    db: Session,
    *,
    city: str | None = None,
    department_id: str | None = None,
    q: str | None = None,
    skip: int = 0,
    limit: int = 100,
) -> list[schemas.AdminOfficerOut]:
    query = (
        db.query(models.User)
        .outerjoin(
            models.Department, models.User.department_id == models.Department.id
        )
        .filter(models.User.role == models.UserRole.OFFICER)
    )
    if city:
        query = query.filter(models.Department.city == city)
    if department_id:
        query = query.filter(models.User.department_id == department_id)
    search = (q or "").strip()
    if search:
        if len(search) > 120:
            raise AdminOperationError(
                "Search text must be 120 characters or fewer.", 422
            )
        pattern = f"%{search}%"
        query = query.filter(
            or_(
                models.User.name.ilike(pattern),
                models.User.email.ilike(pattern),
            )
        )
    rows = (
        query.order_by(models.User.name.asc())
        .offset(max(skip, 0))
        .limit(min(max(limit, 1), 200))
        .all()
    )
    return [officer_out(row) for row in rows]


def list_directory(
    db: Session,
    *,
    city: str | None = None,
    category: models.IssueCategory | None = None,
    active: bool | None = None,
    routing_mode: str | None = None,
    q: str | None = None,
) -> list[schemas.AdminDepartmentOut]:
    query = db.query(models.Department)
    if city:
        query = query.filter(models.Department.city == city)
    if category:
        query = query.filter(models.Department.handles_category == category)
    if active is not None:
        query = query.filter(models.Department.is_active.is_(active))
    if routing_mode:
        query = query.filter(models.Department.routing_mode == routing_mode)
    search = (q or "").strip()
    if search:
        pattern = f"%{search}%"
        query = query.filter(
            or_(
                models.Department.name.ilike(pattern),
                models.Department.short_name.ilike(pattern),
                models.Department.source_agency.ilike(pattern),
            )
        )
    rows = (
        query.order_by(
            models.Department.city.asc(),
            models.Department.name.asc(),
        )
        .all()
    )
    return [department_out(row) for row in rows]


def require_assignable_department(
    db: Session, department_id: str
) -> models.Department:
    """Validate a department for officer provisioning / reassignment.

    This matches the Task 9 officer-provisioning policy: the department must
    already exist, be active and belong to a supported operating city. It does
    not require directory verification metadata, because the pre-existing
    Bengaluru directory rows predate that metadata and are still valid officer
    departments. No department is ever created here.
    """
    department = (
        db.query(models.Department)
        .filter(models.Department.id == department_id)
        .first()
    )
    if department is None:
        raise AdminOperationError(
            "Selected department does not exist. No department was created.", 404
        )
    if not department.is_active:
        raise AdminOperationError("Selected department is inactive.", 400)
    if department.city not in SUPPORTED_OFFICER_CITIES:
        raise AdminOperationError(
            "Selected department is not in a supported operating city.", 400
        )
    return department


# ---------------------------------------------------------------------------
# Audit log reads
# ---------------------------------------------------------------------------
def parse_audit_datetime(value: str | None, *, end_of_day: bool = False):
    """Parse an ISO date/datetime filter into a naive UTC datetime."""
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        if len(cleaned) == 10:
            parsed = datetime.fromisoformat(cleaned)
            if end_of_day:
                parsed = parsed + timedelta(days=1) - timedelta(microseconds=1)
            return parsed
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError as exc:
        raise AdminOperationError(
            "Date filters must be ISO-8601 dates or datetimes.", 422
        ) from exc


def list_audit_logs(
    db: Session,
    *,
    action: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor_user_id: str | None = None,
    city: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> schemas.AdminAuditLogPageOut:
    query = db.query(models.AdminAuditLog)
    if action:
        query = query.filter(models.AdminAuditLog.action == action)
    if entity_type:
        query = query.filter(models.AdminAuditLog.entity_type == entity_type)
    if entity_id:
        query = query.filter(models.AdminAuditLog.entity_id == entity_id)
    if actor_user_id:
        query = query.filter(models.AdminAuditLog.actor_user_id == actor_user_id)
    if city:
        query = query.filter(models.AdminAuditLog.city == city)

    start = parse_audit_datetime(date_from)
    end = parse_audit_datetime(date_to, end_of_day=True)
    if start is not None:
        query = query.filter(models.AdminAuditLog.created_at >= start)
    if end is not None:
        query = query.filter(models.AdminAuditLog.created_at <= end)
    if start is not None and end is not None and start > end:
        raise AdminOperationError(
            "The start date must be earlier than the end date.", 422
        )

    total = query.count()
    rows = (
        query.order_by(
            models.AdminAuditLog.created_at.desc(),
            models.AdminAuditLog.id.desc(),
        )
        .offset(max(skip, 0))
        .limit(min(max(limit, 1), 200))
        .all()
    )
    items = [
        schemas.AdminAuditLogOut(
            id=row.id,
            actor_user_id=row.actor_user_id,
            actor_name=row.actor.name if row.actor else None,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            before_state=row.before_state,
            after_state=row.after_state,
            reason=row.reason,
            city=row.city,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return schemas.AdminAuditLogPageOut(
        items=items,
        total=total,
        skip=max(skip, 0),
        limit=min(max(limit, 1), 200),
    )
