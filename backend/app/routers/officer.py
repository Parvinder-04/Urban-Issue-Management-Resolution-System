"""Department-scoped officer portal API.

Every endpoint here is restricted to authenticated ``OFFICER`` accounts. The
officer's department and city come from the database user, never from a query
parameter. Queue reads include only live complaints that were verified-routed
to that exact department and city with ``routing_status = assigned``.

Historical records, ``unassigned_review`` and ``unassigned_no_mapping``
complaints are intentionally invisible here. They remain with administrator /
manual jurisdiction review and cannot be claimed through this router.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status as http_status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db
from app.routers.auth import require_role
from app.services.officer_workflow import (
    ACTIVE_ISSUE_STATUSES,
    IssueAlreadyClaimedError,
    IssueNotEligibleError,
    OfficerScopeError,
    base_queue_query,
    claim_issue,
    get_actionable_issue,
    officer_department,
)

router = APIRouter(prefix="/officer", tags=["officer"])

officer_required = require_role([models.UserRole.OFFICER])

QueueScope = Literal["queue", "unclaimed", "mine", "all"]


def _department_out(department: models.Department) -> schemas.OfficerDepartmentOut:
    return schemas.OfficerDepartmentOut(
        id=department.id,
        name=department.name,
        short_name=department.short_name,
        city=department.city,
        handles_category=(
            department.handles_category.value
            if isinstance(department.handles_category, models.IssueCategory)
            else department.handles_category
        ),
        routing_mode=department.routing_mode,
    )


def _scope_error(exc: OfficerScopeError) -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_403_FORBIDDEN,
        detail=str(exc),
    )


@router.get("/overview", response_model=schemas.OfficerOverviewOut)
def officer_overview(
    current_user: models.User = Depends(officer_required),
    db: Session = Depends(get_db),
):
    """Real, live, department-scoped operational counts.

    No response time or completion metric is calculated: live resolution
    timestamps exist only for complaints resolved through this workflow, so a
    department-wide average would be misleading. The response says so
    explicitly instead of inventing a figure.
    """
    try:
        department = officer_department(db, current_user)
    except OfficerScopeError as exc:
        raise _scope_error(exc) from exc

    base = base_queue_query(db, department)

    total_live_routed = base.count()
    new_reported = base.filter(
        models.Issue.status == models.IssueStatus.REPORTED
    ).count()
    unclaimed = base.filter(
        models.Issue.assigned_officer_id.is_(None),
        models.Issue.status.in_(ACTIVE_ISSUE_STATUSES),
    ).count()
    assigned_to_me = base.filter(
        models.Issue.assigned_officer_id == current_user.id
    ).count()
    my_open = base.filter(
        models.Issue.assigned_officer_id == current_user.id,
        models.Issue.status.in_(ACTIVE_ISSUE_STATUSES),
    ).count()
    in_progress = base.filter(
        models.Issue.status == models.IssueStatus.IN_PROGRESS
    ).count()
    resolved = base.filter(
        models.Issue.status == models.IssueStatus.RESOLVED
    ).count()

    return schemas.OfficerOverviewOut(
        department=_department_out(department),
        counts=schemas.OfficerOverviewCounts(
            total_live_routed=total_live_routed,
            new_reported=new_reported,
            unclaimed=unclaimed,
            assigned_to_me=assigned_to_me,
            my_open=my_open,
            in_progress=in_progress,
            resolved=resolved,
            # Officers are not authorised to see the manual-review queue in
            # Task 9, so no figure is returned or displayed.
            awaiting_review=None,
            awaiting_review_visible=False,
        ),
        resolution_timing_available=False,
        note=(
            "Operational counts include only live complaints with verified "
            "automatic routing to this department and city. Historical records "
            "and manual-review complaints are excluded. Department-wide "
            "resolution-time averages are not shown because historical "
            "resolution timestamps are unavailable."
        ),
    )


@router.get("/issues", response_model=list[schemas.IssueOut])
def list_officer_issues(
    scope: QueueScope = "queue",
    status: Optional[models.IssueStatus] = None,
    category: Optional[models.IssueCategory] = None,
    priority: Optional[models.PriorityLevel] = None,
    q: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    current_user: models.User = Depends(officer_required),
    db: Session = Depends(get_db),
):
    """List the officer's live routed department queue.

    ``scope`` is a server-enforced view, not a client-side filter:
      * ``queue`` (default) — open/active complaints;
      * ``unclaimed`` — active complaints nobody has claimed;
      * ``mine`` — every complaint assigned to this officer (including closed);
      * ``all`` — every live routed complaint in the department (including closed).

    A ``status``/``category``/``priority`` filter can only narrow that scope.
    """
    try:
        department = officer_department(db, current_user)
    except OfficerScopeError as exc:
        raise _scope_error(exc) from exc

    query = base_queue_query(db, department)

    if scope == "queue":
        query = query.filter(models.Issue.status.in_(ACTIVE_ISSUE_STATUSES))
    elif scope == "unclaimed":
        query = query.filter(
            models.Issue.assigned_officer_id.is_(None),
            models.Issue.status.in_(ACTIVE_ISSUE_STATUSES),
        )
    elif scope == "mine":
        query = query.filter(models.Issue.assigned_officer_id == current_user.id)
    # scope == "all": no additional status/ownership restriction.

    if status:
        query = query.filter(models.Issue.status == status)
    if category:
        query = query.filter(models.Issue.category == category)
    if priority:
        query = query.filter(models.Issue.priority_level == priority)

    search = (q or "").strip()
    if search:
        if len(search) > 120:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Search text must be 120 characters or fewer.",
            )
        pattern = f"%{search}%"
        query = query.filter(
            or_(
                models.Issue.ticket_number.ilike(pattern),
                models.Issue.title.ilike(pattern),
                models.Issue.description.ilike(pattern),
            )
        )

    return (
        query.order_by(
            models.Issue.priority_score.desc(),
            models.Issue.created_at.desc(),
        )
        .offset(max(skip, 0))
        .limit(min(max(limit, 1), 200))
        .all()
    )


@router.get("/issues/{issue_id}", response_model=schemas.OfficerIssueDetailOut)
def get_officer_issue(
    issue_id: str,
    current_user: models.User = Depends(officer_required),
    db: Session = Depends(get_db),
):
    """Return one live routed complaint with its real evidence and audit trail."""
    try:
        department = officer_department(db, current_user)
        issue = get_actionable_issue(db, department, issue_id)
    except OfficerScopeError as exc:
        raise _scope_error(exc) from exc
    except IssueNotEligibleError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

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

    assigned_to_me = issue.assigned_officer_id == current_user.id
    assigned_to_another = bool(
        issue.assigned_officer_id and issue.assigned_officer_id != current_user.id
    )
    can_claim = (
        issue.assigned_officer_id is None
        and issue.status in ACTIVE_ISSUE_STATUSES
    )
    can_update = assigned_to_me and issue.status in ACTIVE_ISSUE_STATUSES

    return schemas.OfficerIssueDetailOut(
        issue=schemas.IssueOut.model_validate(issue),
        department=_department_out(department),
        images=[schemas.IssueImageOut.model_validate(image) for image in images],
        history=[
            schemas.IssueStatusUpdateOut.model_validate(entry) for entry in history
        ],
        permissions=schemas.OfficerIssuePermissions(
            can_claim=can_claim,
            can_update=can_update,
            is_assigned_to_me=assigned_to_me,
            assigned_to_another_officer=assigned_to_another,
        ),
    )


@router.post(
    "/issues/{issue_id}/claim",
    response_model=schemas.OfficerClaimResponse,
)
def claim_officer_issue(
    issue_id: str,
    current_user: models.User = Depends(officer_required),
    db: Session = Depends(get_db),
):
    """Claim an eligible complaint atomically.

    The response is idempotent for the same officer. A different officer
    receives HTTP 409 and the existing owner is never overwritten.
    """
    try:
        issue, already = claim_issue(db, current_user, issue_id)
    except OfficerScopeError as exc:
        raise _scope_error(exc) from exc
    except IssueNotEligibleError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except IssueAlreadyClaimedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    return schemas.OfficerClaimResponse(
        issue=schemas.IssueOut.model_validate(issue),
        claimed=True,
        already_claimed_by_you=already,
        message=(
            "This complaint is already assigned to you."
            if already
            else "Complaint claimed successfully."
        ),
    )
