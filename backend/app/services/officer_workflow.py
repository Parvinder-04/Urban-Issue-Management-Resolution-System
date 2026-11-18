"""Department-scoped officer workflow rules.

This module contains the rules that are shared by the officer portal API and
the existing issue status endpoint:

* which complaints are actionable for an officer (live, exact department,
  exact city, verified ``routing_status = assigned``);
* how an officer atomically claims an unclaimed complaint;
* which status transitions are permitted;
* the mandatory-comment rule.

Historical records and manual-review routing outcomes are deliberately outside
every officer workflow query. They remain administrator/jurisdiction-review
material and can never be claimed or updated by an officer through this module.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Query, Session

from app import models
from app.services.classification_service import CITY_BENGALURU, CITY_GURUGRAM
from app.services.department_routing import ROUTING_STATUS_ASSIGNED

SUPPORTED_OFFICER_CITIES = (CITY_BENGALURU, CITY_GURUGRAM)

# Statuses in which a complaint can still be worked. Terminal statuses are kept
# visible in the department history but are never claimable.
ACTIVE_ISSUE_STATUSES = (
    models.IssueStatus.REPORTED,
    models.IssueStatus.ACKNOWLEDGED,
    models.IssueStatus.IN_PROGRESS,
)

TERMINAL_ISSUE_STATUSES = (
    models.IssueStatus.RESOLVED,
    models.IssueStatus.REJECTED,
    models.IssueStatus.DUPLICATE,
)

# Explicit transition policy. A complaint moves forward through the operational
# states and then stops. In particular, a resolved/rejected/duplicate complaint
# can never be moved back to ``reported``. Administrators may update any
# department's complaint but are held to the same transition policy in Task 9.
ALLOWED_TRANSITIONS: dict[models.IssueStatus, tuple[models.IssueStatus, ...]] = {
    models.IssueStatus.REPORTED: (
        models.IssueStatus.ACKNOWLEDGED,
        models.IssueStatus.IN_PROGRESS,
        models.IssueStatus.RESOLVED,
        models.IssueStatus.REJECTED,
        models.IssueStatus.DUPLICATE,
    ),
    models.IssueStatus.ACKNOWLEDGED: (
        models.IssueStatus.IN_PROGRESS,
        models.IssueStatus.RESOLVED,
        models.IssueStatus.REJECTED,
        models.IssueStatus.DUPLICATE,
    ),
    models.IssueStatus.IN_PROGRESS: (
        models.IssueStatus.RESOLVED,
        models.IssueStatus.REJECTED,
        models.IssueStatus.DUPLICATE,
    ),
    models.IssueStatus.RESOLVED: (),
    models.IssueStatus.REJECTED: (),
    models.IssueStatus.DUPLICATE: (),
}


class OfficerWorkflowError(Exception):
    """Base class for expected officer-workflow refusals."""


class OfficerScopeError(OfficerWorkflowError):
    """The officer has no usable, active, supported-city department."""


class IssueNotEligibleError(OfficerWorkflowError):
    """The complaint is not in the officer's live routed queue."""


class IssueAlreadyClaimedError(OfficerWorkflowError):
    """Another officer owns the complaint."""


class MissingCommentError(OfficerWorkflowError):
    """A status update was submitted without a meaningful comment."""


class InvalidTransitionError(OfficerWorkflowError):
    """The requested status transition is not permitted."""


def officer_department(db: Session, officer: models.User) -> models.Department:
    """Return the officer's active department, or raise a scope error.

    Identity always comes from the authenticated database user. The caller
    never supplies a department id.
    """
    if officer.role != models.UserRole.OFFICER:
        raise OfficerScopeError("Only authenticated officers may use this workflow.")
    if not officer.department_id:
        raise OfficerScopeError("Officer has no assigned department.")

    department = (
        db.query(models.Department)
        .filter(models.Department.id == officer.department_id)
        .first()
    )
    if department is None:
        raise OfficerScopeError("Officer's department no longer exists.")
    if not department.is_active:
        raise OfficerScopeError("Officer's department is not active.")
    if department.city not in SUPPORTED_OFFICER_CITIES:
        raise OfficerScopeError(
            "Officer's department is not attached to a supported operating city."
        )
    return department


def base_queue_query(db: Session, department: models.Department) -> Query:
    """Base query for live, exact-department, exact-city routed complaints.

    The city equality is a second, explicit guard: even a corrupt department_id
    on an issue cannot make another city's complaint appear in this queue.
    """
    return db.query(models.Issue).filter(
        models.Issue.is_historical.is_(False),
        models.Issue.department_id == department.id,
        models.Issue.city == department.city,
        models.Issue.routing_status == ROUTING_STATUS_ASSIGNED,
    )


def get_actionable_issue(
    db: Session, department: models.Department, issue_id: str
) -> models.Issue:
    """Fetch one complaint from the department's live routed queue.

    Historical records, manual-review outcomes, missing mappings and other
    departments/cities are all reported as "not found" so the endpoint never
    confirms that an out-of-scope record exists.
    """
    issue = (
        base_queue_query(db, department)
        .filter(models.Issue.id == issue_id)
        .first()
    )
    if issue is None:
        raise IssueNotEligibleError(
            "Complaint is not in this department's live routed queue."
        )
    return issue


def normalize_comment(comment: str | None) -> str:
    """Return a non-empty, stripped comment or raise :class:`MissingCommentError`."""
    value = (comment or "").strip()
    if not value:
        raise MissingCommentError("A non-empty comment is required for every status update.")
    return value


def validate_transition(
    old_status: models.IssueStatus, new_status: models.IssueStatus
) -> None:
    """Raise :class:`InvalidTransitionError` for an unsupported transition."""
    allowed = ALLOWED_TRANSITIONS.get(old_status, ())
    if new_status not in allowed:
        raise InvalidTransitionError(
            f"Invalid status transition: '{old_status.value}' cannot be changed "
            f"to '{new_status.value}'."
        )


def claim_issue(
    db: Session, officer: models.User, issue_id: str
) -> tuple[models.Issue, bool]:
    """Atomically claim an eligible complaint.

    Returns ``(issue, already_claimed_by_you)``. The ownership write is a single
    conditional ``UPDATE`` so two officers racing for the same complaint cannot
    both succeed: the second statement updates zero rows and receives a clear
    conflict instead of overwriting the first officer.
    """
    department = officer_department(db, officer)
    issue = get_actionable_issue(db, department, issue_id)

    if issue.assigned_officer_id == officer.id:
        return issue, True
    if issue.assigned_officer_id:
        raise IssueAlreadyClaimedError(
            "This complaint has already been claimed by another officer."
        )
    if issue.status not in ACTIVE_ISSUE_STATUSES:
        raise IssueNotEligibleError(
            "Closed complaints cannot be claimed."
        )

    now = datetime.now(timezone.utc)
    updated = (
        db.query(models.Issue)
        .filter(
            models.Issue.id == issue_id,
            models.Issue.is_historical.is_(False),
            models.Issue.department_id == department.id,
            models.Issue.city == department.city,
            models.Issue.routing_status == ROUTING_STATUS_ASSIGNED,
            models.Issue.status.in_(ACTIVE_ISSUE_STATUSES),
            models.Issue.assigned_officer_id.is_(None),
        )
        .update(
            {
                models.Issue.assigned_officer_id: officer.id,
                models.Issue.updated_at: now,
            },
            synchronize_session=False,
        )
    )

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    if updated == 1:
        db.expire_all()
        fresh = db.query(models.Issue).filter(models.Issue.id == issue_id).first()
        if fresh is None:  # pragma: no cover - deleted between statements
            raise IssueNotEligibleError("Complaint no longer exists.")
        return fresh, False

    # The conditional update lost a race (or the record changed). Re-read the
    # truth and report a precise, non-destructive outcome.
    db.expire_all()
    current = db.query(models.Issue).filter(models.Issue.id == issue_id).first()
    if current is None:
        raise IssueNotEligibleError("Complaint no longer exists.")
    if current.assigned_officer_id == officer.id:
        return current, True
    if current.assigned_officer_id:
        raise IssueAlreadyClaimedError(
            "This complaint has already been claimed by another officer."
        )
    raise IssueNotEligibleError(
        "This complaint is no longer eligible to be claimed."
    )
