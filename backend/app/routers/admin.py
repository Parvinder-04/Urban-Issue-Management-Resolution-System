"""Administrator portal API.

Every endpoint in this router is restricted to authenticated accounts whose
database role is ``ADMIN``. The dependency runs independently of any client-side
role check: citizens and officers receive HTTP 403, invalid/expired tokens
receive HTTP 401, and the actor identity always comes from the verified JWT and
database user.

The router never exposes password hashes, tokens or citizen contact details. It
does not offer any endpoint that updates or deletes an audit entry.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db
from app.routers.auth import require_role
from app.services.admin_audit import (
    ACTION_ISSUE_LEFT_UNASSIGNED,
    ACTION_ISSUE_ROUTED,
    ACTION_OFFICER_CREATED,
    ACTION_OFFICER_DEPARTMENT_CHANGED,
    ENTITY_USER,
    officer_department_state,
    record_admin_audit,
)
from app.services.admin_operations import (
    AdminOperationError,
    admin_issue_out,
    build_overview,
    get_admin_issue_detail,
    leave_issue_unassigned,
    list_admin_issues,
    list_audit_logs,
    list_directory,
    list_officers,
    list_review_queue,
    officer_out,
    require_assignable_department,
    route_issue,
)
from app.services.officer_provisioning import (
    OfficerProvisioningError,
    provision_officer,
)

router = APIRouter(prefix="/admin", tags=["admin"])

admin_required = require_role([models.UserRole.ADMIN])


def _operation_error(exc: AdminOperationError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


# ---------------------------------------------------------------------------
# Overview and live complaints
# ---------------------------------------------------------------------------
@router.get("/overview", response_model=schemas.AdminOverviewOut)
def admin_overview(
    city: Optional[str] = Query(default=None, max_length=120),
    department_id: Optional[str] = Query(default=None, max_length=36),
    date_from: Optional[str] = Query(default=None, max_length=40),
    date_to: Optional[str] = Query(default=None, max_length=40),
    current_user: models.User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Real operational and historical counts, never mixed together."""
    try:
        return build_overview(
            db,
            city=city,
            department_id=department_id,
            date_from=date_from,
            date_to=date_to,
        )
    except AdminOperationError as exc:
        raise _operation_error(exc) from exc


@router.get("/issues", response_model=list[schemas.AdminIssueOut])
def admin_list_issues(
    scope: Literal["live", "historical", "all"] = "live",
    status: Optional[models.IssueStatus] = None,
    category: Optional[models.IssueCategory] = None,
    priority: Optional[models.PriorityLevel] = None,
    city: Optional[str] = Query(default=None, max_length=120),
    department_id: Optional[str] = Query(default=None, max_length=36),
    routing_status: Optional[str] = Query(default=None, max_length=30),
    q: Optional[str] = Query(default=None, max_length=120),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: models.User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """List live or historical complaints with server-side filters."""
    try:
        return list_admin_issues(
            db,
            scope=scope,
            status=status,
            category=category,
            priority=priority,
            city=city,
            department_id=department_id,
            routing_status=routing_status,
            q=q,
            skip=skip,
            limit=limit,
        )
    except AdminOperationError as exc:
        raise _operation_error(exc) from exc


@router.get("/issues/{issue_id}", response_model=schemas.AdminIssueDetailOut)
def admin_issue_detail(
    issue_id: str,
    current_user: models.User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Return one complaint with evidence, history and real routing candidates."""
    try:
        return get_admin_issue_detail(db, issue_id)
    except AdminOperationError as exc:
        raise _operation_error(exc) from exc


# ---------------------------------------------------------------------------
# Manual jurisdiction review
# ---------------------------------------------------------------------------
@router.get("/review-queue", response_model=list[schemas.AdminIssueOut])
def admin_review_queue(
    city: Optional[str] = Query(default=None, max_length=120),
    category: Optional[models.IssueCategory] = None,
    priority: Optional[models.PriorityLevel] = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: models.User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Live complaints awaiting a documented jurisdiction decision."""
    return list_review_queue(
        db,
        city=city,
        category=category,
        priority=priority,
        skip=skip,
        limit=limit,
    )


@router.post(
    "/issues/{issue_id}/route",
    response_model=schemas.AdminRoutingResponse,
)
def admin_route_issue(
    issue_id: str,
    payload: schemas.AdminRouteRequest,
    current_user: models.User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Route to a verified department, or keep unassigned with a reason.

    A valid department assignment sets ``department_id`` and
    ``routing_status = assigned``. It never assigns an officer; the existing
    officer claim workflow remains the only way ownership changes.
    """
    department_id = (payload.department_id or "").strip() or None
    try:
        if department_id:
            issue, audit = route_issue(
                db,
                actor=current_user,
                issue_id=issue_id,
                department_id=department_id,
                reason=payload.reason,
            )
            action = ACTION_ISSUE_ROUTED
            message = "Complaint routed to the selected verified department."
        else:
            issue, audit = leave_issue_unassigned(
                db,
                actor=current_user,
                issue_id=issue_id,
                reason=payload.reason,
            )
            action = ACTION_ISSUE_LEFT_UNASSIGNED
            message = "Complaint kept unassigned with a documented reason."
    except AdminOperationError as exc:
        raise _operation_error(exc) from exc

    return schemas.AdminRoutingResponse(
        issue=admin_issue_out(issue),
        action=action,
        audit_id=audit.id,
        message=message,
    )


# ---------------------------------------------------------------------------
# Officer management
# ---------------------------------------------------------------------------
@router.get("/officers", response_model=list[schemas.AdminOfficerOut])
def admin_list_officers(
    city: Optional[str] = Query(default=None, max_length=120),
    department_id: Optional[str] = Query(default=None, max_length=36),
    q: Optional[str] = Query(default=None, max_length=120),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    current_user: models.User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """List real OFFICER accounts. Password hashes are never serialized."""
    try:
        return list_officers(
            db,
            city=city,
            department_id=department_id,
            q=q,
            skip=skip,
            limit=limit,
        )
    except AdminOperationError as exc:
        raise _operation_error(exc) from exc


@router.post(
    "/officers",
    response_model=schemas.AdminOfficerOut,
    status_code=http_status.HTTP_201_CREATED,
)
def admin_create_officer(
    payload: schemas.AdminOfficerCreate,
    current_user: models.User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Provision one OFFICER account through the controlled service.

    The role is forced to OFFICER by the service. Duplicate e-mail addresses are
    rejected and an existing user is never modified. The password is hashed with
    bcrypt and is never returned. The account creation and its audit event share
    one transaction.
    """
    try:
        department = require_assignable_department(db, payload.department_id)
        provisioned = provision_officer(
            db,
            name=payload.name,
            email=str(payload.email),
            phone=payload.phone,
            department_id=department.id,
            password=payload.password,
            preferred_language=payload.preferred_language,
            commit=False,
        )
    except (AdminOperationError, OfficerProvisioningError) as exc:
        db.rollback()
        if isinstance(exc, AdminOperationError):
            raise _operation_error(exc) from exc
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    audit = record_admin_audit(
        db,
        actor=current_user,
        action=ACTION_OFFICER_CREATED,
        entity_type=ENTITY_USER,
        entity_id=provisioned.id,
        before_state=None,
        after_state={
            "user_id": provisioned.id,
            "role": provisioned.role,
            "department_id": provisioned.department_id,
            "department_city": provisioned.department_city,
        },
        reason="Administrator provisioned an officer account.",
        city=provisioned.department_city,
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    user = db.query(models.User).filter(models.User.id == provisioned.id).first()
    if user is None:  # pragma: no cover - created and committed above
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Officer account could not be reloaded.",
        )
    db.refresh(user)
    response = officer_out(user)
    # The audit id is not part of the officer response contract, but the row is
    # committed atomically with the account. ``audit`` is intentionally unused.
    _ = audit
    return response


@router.patch(
    "/officers/{officer_id}/department",
    response_model=schemas.AdminOfficerOut,
)
def admin_change_officer_department(
    officer_id: str,
    payload: schemas.AdminOfficerDepartmentUpdate,
    current_user: models.User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Reassign an existing OFFICER to a verified active department.

    Citizens and administrators are never modified here. Historical issues keep
    their original department ids; only the user's future queue scope changes.
    """
    user = db.query(models.User).filter(models.User.id == officer_id).first()
    if user is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Officer not found."
        )
    if user.role != models.UserRole.OFFICER:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Only officer accounts can be reassigned through this endpoint.",
        )

    try:
        department = require_assignable_department(db, payload.department_id)
    except AdminOperationError as exc:
        raise _operation_error(exc) from exc

    if user.department_id == department.id:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Officer is already assigned to this department.",
        )

    before = officer_department_state(user)
    user.department_id = department.id
    after = officer_department_state(user)
    record_admin_audit(
        db,
        actor=current_user,
        action=ACTION_OFFICER_DEPARTMENT_CHANGED,
        entity_type=ENTITY_USER,
        entity_id=user.id,
        before_state=before,
        after_state=after,
        reason=payload.reason,
        city=department.city,
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(user)
    return officer_out(user)


# ---------------------------------------------------------------------------
# Verified department directory (read-only)
# ---------------------------------------------------------------------------
@router.get("/departments", response_model=list[schemas.AdminDepartmentOut])
def admin_directory(
    city: Optional[str] = Query(default=None, max_length=120),
    category: Optional[models.IssueCategory] = None,
    active: Optional[bool] = None,
    routing_mode: Optional[str] = Query(default=None, max_length=20),
    q: Optional[str] = Query(default=None, max_length=120),
    current_user: models.User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Read-only verified directory. No mutation endpoint exists in Task 10."""
    return list_directory(
        db,
        city=city,
        category=category,
        active=active,
        routing_mode=routing_mode,
        q=q,
    )


# ---------------------------------------------------------------------------
# Administrative audit log (append-only)
# ---------------------------------------------------------------------------
@router.get("/audit-logs", response_model=schemas.AdminAuditLogPageOut)
def admin_audit_logs(
    action: Optional[str] = Query(default=None, max_length=64),
    entity_type: Optional[str] = Query(default=None, max_length=40),
    entity_id: Optional[str] = Query(default=None, max_length=36),
    actor_user_id: Optional[str] = Query(default=None, max_length=36),
    city: Optional[str] = Query(default=None, max_length=120),
    date_from: Optional[str] = Query(default=None, max_length=40),
    date_to: Optional[str] = Query(default=None, max_length=40),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: models.User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Paginated read-only audit trail with server-side filters."""
    try:
        return list_audit_logs(
            db,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_user_id=actor_user_id,
            city=city,
            date_from=date_from,
            date_to=date_to,
            skip=skip,
            limit=limit,
        )
    except AdminOperationError as exc:
        raise _operation_error(exc) from exc
