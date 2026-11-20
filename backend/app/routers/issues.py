"""
Issues Router — Core pipeline of the system.
Secured with JWT authentication, RBAC authorization, and strict historical record protection.
"""
import os
import random
import string
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models, schemas
from app.core.config import get_settings
from app.database import get_db
from app.routers.auth import get_current_user, require_role
from app.services.classification_service import (
    IssueClassifier,
    PriorityEngine,
    combine_complaint_text,
    score_kind_for_source,
)
from app.services.evidence_storage import (
    EvidenceStorageError,
    EvidenceTooLargeError,
    FORMAT_EXTENSIONS,
    UnsupportedImageTypeError,
    compress_image_bytes,
    delete_evidence,
    detect_image_format,
    evidence_storage_backend,
    media_type_for_path,
    resolve_evidence_path,
    store_evidence,
)
from app.services.geo_clustering_service import GeoPoint, find_duplicates
from app.services.image_verification_service import verify_image_bytes
from app.services.department_routing import (
    ROUTING_STATUS_ASSIGNED,
    resolve_department,
)
from app.services.officer_workflow import (
    ACTIVE_ISSUE_STATUSES,
    InvalidTransitionError,
    MissingCommentError,
    normalize_comment,
    validate_transition,
)

router = APIRouter(prefix="/issues", tags=["issues"])
settings = get_settings()

# Models are loaded once at startup, not per-request (expensive to reload).
classifier = IssueClassifier()
priority_engine = PriorityEngine()


def _generate_ticket_number(db: Session) -> str:
    year = datetime.now(timezone.utc).year
    suffix = "".join(random.choices(string.digits, k=5))
    return f"UIMRS-{year}-{suffix}"


# ---------------------------------------------------------------------------
# Read-side authorization
# ---------------------------------------------------------------------------
def _scoped_issue_query(db: Session, current_user: models.User):
    """Base issue query already scoped to what ``current_user`` may read.

    Reporter and department identity always come from the authenticated user,
    never from a query parameter or request body.
    """
    query = db.query(models.Issue)

    if current_user.role == models.UserRole.ADMIN:
        return query

    if current_user.role == models.UserRole.CITIZEN:
        # Citizens may read only their own live submissions. Historical records
        # are public-source data, never a citizen's private complaint.
        return query.filter(
            models.Issue.is_historical == False,  # noqa: E712 - SQLAlchemy expression
            models.Issue.reporter_id == current_user.id,
        )

    if current_user.role == models.UserRole.OFFICER:
        if current_user.department_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Officer has no assigned department.",
            )
        return query.filter(models.Issue.department_id == current_user.department_id)

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Operation not permitted.",
    )


def _assert_can_read_issue(issue: models.Issue, current_user: models.User) -> None:
    """Raise 404 when ``current_user`` may not read ``issue``.

    404 is used instead of 403 for record-level denials so that the endpoint
    does not confirm the existence of another citizen's or department's record.
    """
    if current_user.role == models.UserRole.ADMIN:
        return

    if current_user.role == models.UserRole.CITIZEN:
        if issue.is_historical or issue.reporter_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found"
            )
        return

    if current_user.role == models.UserRole.OFFICER:
        if (
            current_user.department_id is None
            or issue.department_id != current_user.department_id
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found"
            )
        return

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found"
    )


@router.post("", response_model=schemas.IssueOut, status_code=status.HTTP_201_CREATED)
def create_issue(
    payload: schemas.IssueCreate,
    current_user: models.User = Depends(
        require_role([models.UserRole.CITIZEN, models.UserRole.ADMIN])
    ),
    db: Session = Depends(get_db),
):
    """Create a new citizen issue report.

    Reporter identity is securely derived from the authenticated JWT token.
    """
    # 1. City-aware AI classification. Title and description are combined exactly
    # as the V2.2 training pipeline expects; the classifier itself decides whether
    # the Bengaluru model or Indian keyword rules apply.
    combined_text = combine_complaint_text(payload.title, payload.description)
    classification = classifier.classify(combined_text, payload.city)

    # Normalize to the backend enum. An unknown label can never be silently
    # accepted as a different category.
    category = classification.category
    if not isinstance(category, models.IssueCategory):
        try:
            category = models.IssueCategory(category)
        except ValueError:
            category = models.IssueCategory.OTHER

    # ``needs_review``, ``model_version``, ``top_alternatives`` and the priority
    # reasons are now persisted as nullable explainability columns. Historical
    # rows keep NULL; only this live pipeline writes real values.

    # 2. City-aware department routing. The lookup is strictly scoped to the
    # canonical issue city and category: there is deliberately no category-only
    # fallback, so another city's department can never be assigned. Manual-review
    # and missing mappings stay unassigned with a truthful explanation.
    routing = resolve_department(db, payload.city, category)

    # 3. Duplicate detection against LIVE OPEN issues only (never cluster against historical logs)
    nearby_open_issues = (
        db.query(models.Issue)
        .filter(
            models.Issue.category == category,
            models.Issue.is_historical == False,
            models.Issue.status.in_([
                models.IssueStatus.REPORTED,
                models.IssueStatus.ACKNOWLEDGED,
                models.IssueStatus.IN_PROGRESS,
            ]),
            func.abs(models.Issue.latitude - payload.latitude) < 0.01,   # ~1.1km bounding box
            func.abs(models.Issue.longitude - payload.longitude) < 0.01,
        )
        .all()
    )
    candidate_points = [
        GeoPoint(i.id, i.latitude, i.longitude, i.category.value) for i in nearby_open_issues
    ]
    temp_point = GeoPoint("NEW", payload.latitude, payload.longitude, category.value)
    duplicate_ids = find_duplicates(
        temp_point, candidate_points, radius_meters=settings.DUPLICATE_RADIUS_METERS
    )

    duplicate_count = len(duplicate_ids)

    # 4. Priority scoring (duplicate_count feeds in as a signal)
    priority = priority_engine.score(
        payload.description, category.value, duplicate_count=duplicate_count
    )

    # 5. Persist the new live issue
    issue = models.Issue(
        ticket_number=_generate_ticket_number(db),
        title=payload.title,
        description=payload.description,
        category=category,
        category_confidence=classification.confidence,
        category_source=classification.source,
        category_model_version=classification.model_version,
        category_needs_review=classification.needs_review,
        category_review_reason=classification.review_reason,
        # Persist alternatives only when the classifier actually produced them.
        category_top_alternatives=classification.top_alternatives or None,
        category_score_kind=score_kind_for_source(classification.source),
        priority_score=priority.score,
        priority_level=models.PriorityLevel(priority.level),
        # Persist the real PriorityEngine reasons, never an invented explanation.
        priority_reasons=priority.reasons or None,
        status=models.IssueStatus.DUPLICATE if duplicate_ids else models.IssueStatus.REPORTED,
        location=f"POINT({payload.longitude} {payload.latitude})",
        latitude=payload.latitude,
        longitude=payload.longitude,
        address_text=payload.address_text,
        city=payload.city,
        is_historical=False,
        duplicate_of_issue_id=duplicate_ids[0] if duplicate_ids else None,
        duplicate_count=0,
        reporter_id=current_user.id,
        department_id=routing.department.id if routing.department else None,
        routing_status=routing.status,
        routing_reason=routing.reason,
    )
    db.add(issue)

    # Bump duplicate_count + re-score priority on the original issue (strictly non-historical only)
    if duplicate_ids:
        original = db.query(models.Issue).filter(models.Issue.id == duplicate_ids[0]).first()
        if original and not original.is_historical:
            original.duplicate_count = (original.duplicate_count or 0) + 1
            recheck = priority_engine.score(
                original.description,
                original.category.value,
                duplicate_count=original.duplicate_count,
            )
            original.priority_score = recheck.score
            original.priority_level = models.PriorityLevel(recheck.level)
            original.priority_reasons = recheck.reasons or None

    db.commit()
    db.refresh(issue)
    return issue


@router.get("", response_model=list[schemas.IssueOut])
def list_issues(
    status: Optional[models.IssueStatus] = None,
    category: Optional[models.IssueCategory] = None,
    priority: Optional[models.PriorityLevel] = None,
    city: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List issues visible to the authenticated user.

    Citizens see only their own live complaints, officers see only their own
    department, and admins see all records. Filters never widen that scope.
    """
    query = _scoped_issue_query(db, current_user)
    if status:
        query = query.filter(models.Issue.status == status)
    if category:
        query = query.filter(models.Issue.category == category)
    if priority:
        query = query.filter(models.Issue.priority_level == priority)
    if city:
        query = query.filter(models.Issue.city == city)
    return (
        query.order_by(models.Issue.priority_score.desc(), models.Issue.created_at.desc())
        .offset(skip)
        .limit(min(limit, 500))
        .all()
    )


@router.get("/dashboard")
def dashboard_issues(skip: int = 0, limit: int = 200, db: Session = Depends(get_db)):
    """Public historical dashboard feed.

    Deliberately historical-only: live citizen submissions (and their precise
    coordinates, titles and reporter information) are never exposed here. The
    response keeps the existing field contract used by the staging page.
    """
    rows = (
        db.query(models.Issue)
        .filter(models.Issue.is_historical == True)  # noqa: E712 - SQLAlchemy expression
        .order_by(models.Issue.created_at.desc())
        .offset(skip)
        .limit(min(limit, 1000))
        .all()
    )
    return [
        {
            "id": i.id,
            # A generated internal ticket is not presented as a source ticket for historical records.
            "ticket": None if i.is_historical else i.ticket_number,
            "source_record_id": i.source_record_id,
            "title": i.title,
            "category": i.category.value,
            "priority": float(i.priority_score) if i.priority_score is not None else None,
            "level": i.priority_level.value if i.priority_level else None,
            "status": i.status.value,
            "lat": i.latitude,
            "lng": i.longitude,
            "duplicates": int(i.duplicate_count) if i.duplicate_count is not None else None,
            "department": i.department.name if i.department else None,
            "source_agency": i.source_agency,
            "source_dataset": i.source_dataset,
            "source_status": i.source_status,
            "city": i.city,
            "is_historical": bool(i.is_historical),
            "created_at": i.created_at.isoformat() if i.created_at else None,
        }
        for i in rows
    ]


@router.get("/{issue_id}", response_model=schemas.IssueOut)
def get_issue(
    issue_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Fetch a single issue the authenticated user is authorised to read."""
    issue = db.query(models.Issue).filter(models.Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")
    _assert_can_read_issue(issue, current_user)
    return issue


@router.get("/{issue_id}/history", response_model=list[schemas.IssueStatusUpdateOut])
def get_issue_history(
    issue_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retrieve an issue's audit trail under the same read authorization rules."""
    issue = db.query(models.Issue).filter(models.Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")
    _assert_can_read_issue(issue, current_user)
    return (
        db.query(models.IssueStatusUpdate)
        .filter(models.IssueStatusUpdate.issue_id == issue_id)
        .order_by(models.IssueStatusUpdate.created_at.asc())
        .all()
    )



@router.patch("/{issue_id}/status", response_model=schemas.IssueOut)
def update_status(
    issue_id: str,
    payload: schemas.IssueStatusUpdateCreate,
    current_user: models.User = Depends(
        require_role([models.UserRole.OFFICER, models.UserRole.ADMIN])
    ),
    db: Session = Depends(get_db),
):
    """Update issue status with officer/admin authorization and audit trail.

    Historical complaint records are strictly immutable and reject updates with HTTP 403.
    """
    issue = db.query(models.Issue).filter(models.Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")

    # Historical record immutability lock
    if issue.is_historical:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Historical complaint records are immutable and cannot be updated.",
        )

    # Officer department, city, routing and ownership checks. Identity and
    # department always come from the authenticated database user.
    if current_user.role == models.UserRole.OFFICER:
        if current_user.department_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Officer has no assigned department.",
            )
        if issue.department_id is not None and current_user.department_id != issue.department_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Officer not authorized to update issues assigned to another department.",
            )

        department = (
            db.query(models.Department)
            .filter(models.Department.id == current_user.department_id)
            .first()
        )
        if department is None or not department.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Officer's department is missing or inactive.",
            )
        if (
            department.city
            and issue.city
            and issue.city != department.city
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Officer not authorized to update issues from another city.",
            )
        if issue.routing_status != ROUTING_STATUS_ASSIGNED:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Officer can update only complaints with verified automatic routing.",
            )
        if issue.assigned_officer_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Officer must claim this complaint before updating its status.",
            )
        if issue.assigned_officer_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This complaint is assigned to another officer.",
            )
        if issue.status not in ACTIVE_ISSUE_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Closed complaints cannot be updated.",
            )

    try:
        comment = normalize_comment(payload.comment)
    except MissingCommentError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    old_status = issue.status
    try:
        validate_transition(old_status, payload.new_status)
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    issue.status = payload.new_status
    now = datetime.now(timezone.utc)
    issue.updated_at = now
    # ``resolved_at`` is set only for an actual RESOLVED transition. It is never
    # written for reported/acknowledged/in-progress complaints.
    if payload.new_status == models.IssueStatus.RESOLVED:
        issue.resolved_at = now

    # Record in audit trail
    status_update = models.IssueStatusUpdate(
        issue_id=issue.id,
        updated_by_id=current_user.id,
        old_status=old_status,
        new_status=payload.new_status,
        comment=comment,
        created_at=now,
    )
    db.add(status_update)
    db.commit()
    db.refresh(issue)
    return issue


@router.post("/{issue_id}/images", response_model=schemas.IssueImageOut, status_code=status.HTTP_201_CREATED)
async def upload_issue_image(
    issue_id: str,
    file: UploadFile = File(...),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload and verify an evidence image for a live issue report.

    Security properties:
      * historical records are rejected before any file handling;
      * citizens may attach evidence only to their own complaints;
      * the size limit is enforced while reading, not after buffering;
      * the format is detected from magic bytes and the bytes must decode;
      * the client filename and MIME type are never used for storage;
      * a database failure removes the just-written file.
    """
    issue = db.query(models.Issue).filter(models.Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")

    if issue.is_historical:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot attach images to historical complaint records.",
        )

    if current_user.role == models.UserRole.CITIZEN and issue.reporter_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Citizens can only upload images to their own reported issues.",
        )

    max_bytes = int(settings.MAX_UPLOAD_SIZE_MB) * 1024 * 1024
    # Read one byte past the limit so oversized uploads are detected without
    # buffering an unbounded request body in memory.
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Evidence image exceeds the {settings.MAX_UPLOAD_SIZE_MB} MB limit.",
        )
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    detected_format = detect_image_format(content)
    if detected_format is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Unsupported file type. Only JPEG, PNG and WebP images are accepted.",
        )

    result = verify_image_bytes(content)
    if result.resolution == (0, 0):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The uploaded file is not a valid decodable image.",
        )

    backend = evidence_storage_backend()
    stored = None
    storage_key: str
    image_data: bytes | None = None

    if backend == "database":
        # Keep the database small on the free tier: resize large images and
        # re-encode them before storing the bytes. Verification already ran on
        # the original bytes, so quality checks are unaffected.
        image_data = compress_image_bytes(content, detected_format)
        image_id = str(uuid.uuid4())
        extension = FORMAT_EXTENSIONS[detected_format]
        storage_key = f"db:{image_id}{extension}"
    else:
        try:
            stored = store_evidence(content, detected_format)
        except EvidenceTooLargeError as exc:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)
            ) from exc
        except UnsupportedImageTypeError as exc:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
            ) from exc
        except EvidenceStorageError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not store the uploaded evidence image.",
            ) from exc
        storage_key = stored.storage_key

    image = models.IssueImage(
        id=image_id if backend == "database" else None,
        issue_id=issue_id,
        # Store the relative storage key, never an absolute filesystem path.
        # For database-backed storage this is a ``db:<uuid>.ext`` sentinel.
        image_url=storage_key,
        image_data=image_data,
        blur_score=result.blur_score,
        brightness=result.brightness,
        resolution_width=result.resolution[0],
        resolution_height=result.resolution[1],
        passed_verification=result.passed,
        perceptual_hash=result.phash,
    )
    try:
        db.add(image)
        db.commit()
        db.refresh(image)
    except Exception:
        db.rollback()
        if backend != "database" and stored is not None:
            delete_evidence(stored.storage_key)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not save the uploaded evidence image.",
        )
    return image


@router.get("/{issue_id}/images", response_model=list[schemas.IssueImageOut])
def list_issue_images(
    issue_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List evidence metadata for an issue the caller may read.

    Metadata only: citizens see their own live complaints, officers see their
    department and admins see all. Image bytes require the authenticated
    content endpoint below.
    """
    issue = db.query(models.Issue).filter(models.Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")
    _assert_can_read_issue(issue, current_user)

    return (
        db.query(models.IssueImage)
        .filter(models.IssueImage.issue_id == issue.id)
        .order_by(models.IssueImage.uploaded_at.asc())
        .all()
    )


@router.get("/{issue_id}/images/{image_id}/content")
def get_issue_image_content(
    issue_id: str,
    image_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Serve one evidence photograph to an authorised caller.

    Evidence is never exposed through a public static directory. Every request
    re-checks issue-level authorization and the stored key is resolved through
    the traversal-safe evidence storage resolver.
    """
    issue = db.query(models.Issue).filter(models.Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")
    _assert_can_read_issue(issue, current_user)

    image = (
        db.query(models.IssueImage)
        .filter(
            models.IssueImage.id == image_id,
            models.IssueImage.issue_id == issue.id,
        )
        .first()
    )
    if not image:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evidence image not found")

    if image.image_url.startswith("db:"):
        # Database-backed evidence: bytes live in the application database.
        suffix = image.image_url[len("db:"):]
        media_type = media_type_for_path(Path(suffix))
        if not image.image_data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Evidence image not available",
            )
        return Response(
            content=image.image_data,
            media_type=media_type,
            headers={
                "Content-Disposition": f'inline; filename="{Path(suffix).name}"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    try:
        path = resolve_evidence_path(image.image_url)
    except EvidenceStorageError:
        # Legacy absolute paths and malformed keys are never served.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Evidence image not available",
        )

    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Evidence image not available",
        )

    return FileResponse(
        path,
        media_type=media_type_for_path(path),
        headers={
            "Content-Disposition": f'inline; filename="{path.name}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
