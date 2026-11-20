"""
Analytics for UIMRS.

Historical resolution metrics are unavailable when the source does not provide
actual resolution timestamps. Hotspots are computed from recorded coordinates.
"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, case

from app.database import get_db
from app import models, schemas
from app.services.geo_clustering_service import GeoPoint, find_hotspots
from app.core.config import get_settings

router = APIRouter(prefix="/analytics", tags=["analytics"])
settings = get_settings()


def _parse_filter_date(value: Optional[str], *, end_of_day: bool = False):
    """Parse an optional ISO-8601 date/datetime filter into a naive datetime."""
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="date_from and date_to must be ISO-8601 dates.",
        ) from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    if len(cleaned) == 10 and end_of_day:
        parsed = parsed + timedelta(days=1) - timedelta(microseconds=1)
    return parsed


def _historical_filters(
    *, date_from: Optional[str], date_to: Optional[str]
):
    """Return the SQLAlchemy filters shared by every public aggregate."""
    filters = [models.Issue.is_historical == True]  # noqa: E712
    start = _parse_filter_date(date_from)
    end = _parse_filter_date(date_to, end_of_day=True)
    if start is not None and end is not None and start > end:
        raise HTTPException(
            status_code=422, detail="date_from must not be later than date_to."
        )
    if start is not None:
        filters.append(models.Issue.created_at >= start)
    if end is not None:
        filters.append(models.Issue.created_at <= end)
    return filters


@router.get("/dashboard", response_model=schemas.DashboardSummary)
def dashboard_summary(
    date_from: Optional[str] = Query(default=None, max_length=40),
    date_to: Optional[str] = Query(default=None, max_length=40),
    db: Session = Depends(get_db),
):
    """Public historical aggregate.

    All queries are restricted to historical records so public analytics cannot
    reveal live citizen submissions, counts or precise reporting coordinates.
    Optional ``date_from``/``date_to`` ISO-8601 filters narrow the historical
    window; they never widen access to live rows.
    """
    historical = _historical_filters(date_from=date_from, date_to=date_to)

    total_issues = db.query(models.Issue).filter(*historical).count()

    open_issues = (
        db.query(models.Issue)
        .filter(*historical)
        .filter(models.Issue.status.notin_([
            models.IssueStatus.RESOLVED,
            models.IssueStatus.REJECTED,
            models.IssueStatus.DUPLICATE,
        ]))
        .count()
    )

    # Calculate resolution metrics only when actual timestamps exist.
    resolved_issues = (
        db.query(models.Issue)
        .filter(*historical)
        .filter(
            models.Issue.status == models.IssueStatus.RESOLVED,
            models.Issue.resolved_at.isnot(None),
            models.Issue.created_at.isnot(None),
        )
        .all()
    )

    if resolved_issues:
        one_week_ago = datetime.utcnow() - timedelta(days=7)
        resolved_this_week = sum(
            1 for issue in resolved_issues
            if issue.resolved_at >= one_week_ago
        )
        avg_hours = sum(
            (i.resolved_at - i.created_at).total_seconds() / 3600
            for i in resolved_issues
        ) / len(resolved_issues)
    else:
        resolved_this_week = None
        avg_hours = None

    category_rows = (
        db.query(models.Issue.category, func.count(models.Issue.id))
        .filter(*historical)
        .group_by(models.Issue.category)
        .all()
    )

    category_breakdown = []
    for category, count in category_rows:
        resolved_cat = [
            i for i in resolved_issues
            if i.category == category and i.created_at and i.resolved_at
        ]
        avg_cat = (
            sum(
                (i.resolved_at - i.created_at).total_seconds() / 3600
                for i in resolved_cat
            ) / len(resolved_cat)
            if resolved_cat else None
        )
        category_breakdown.append(
            schemas.CategoryBreakdown(
                category=category.value,
                count=count,
                avg_resolution_hours=round(avg_cat, 1) if avg_cat is not None else None,
            )
        )

    open_rows = (
        db.query(models.Issue)
        .filter(*historical)
        .filter(models.Issue.status.notin_([
            models.IssueStatus.RESOLVED,
            models.IssueStatus.REJECTED,
            models.IssueStatus.DUPLICATE,
        ]))
        .all()
    )
    points = [
        GeoPoint(i.id, i.latitude, i.longitude, i.category.value)
        for i in open_rows
        if i.latitude is not None and i.longitude is not None
    ]
    clusters = find_hotspots(
        points,
        radius_meters=settings.HOTSPOT_RADIUS_METERS,
        min_issues=settings.HOTSPOT_MIN_ISSUES,
    )
    hotspots = [
        schemas.HotspotOut(
            cluster_id=c.cluster_id,
            center_lat=c.center_lat,
            center_lon=c.center_lon,
            issue_count=c.size,
            categories=list({p.category for p in c.points}),
        )
        for c in clusters
    ]

    return schemas.DashboardSummary(
        total_issues=total_issues,
        open_issues=open_issues,
        resolved_this_week=resolved_this_week,
        avg_resolution_hours=round(avg_hours, 1) if avg_hours is not None else None,
        category_breakdown=category_breakdown,
        hotspots=hotspots,
    )


@router.get("/departments")
def department_summary(
    date_from: Optional[str] = Query(default=None, max_length=40),
    date_to: Optional[str] = Query(default=None, max_length=40),
    db: Session = Depends(get_db),
):
    historical = _historical_filters(date_from=date_from, date_to=date_to)
    # Group category-specific department rows by the actual recorded agency.
    rows = (
        db.query(
            models.Department.name,
            models.Department.city,
            models.Department.source_agency,
            func.count(models.Issue.id).label("records"),
            func.sum(case(
                (models.Issue.status == models.IssueStatus.REPORTED, 1), else_=0
            )).label("reported"),
            func.sum(case(
                (models.Issue.status == models.IssueStatus.IN_PROGRESS, 1), else_=0
            )).label("in_progress"),
            func.sum(case(
                (models.Issue.status == models.IssueStatus.RESOLVED, 1), else_=0
            )).label("resolved"),
            func.sum(case(
                (models.Issue.status == models.IssueStatus.REJECTED, 1), else_=0
            )).label("rejected"),
        )
        .join(models.Issue, models.Issue.department_id == models.Department.id)
        .filter(*historical)
        .group_by(
            models.Department.name,
            models.Department.city,
            models.Department.source_agency,
        )
        .order_by(func.count(models.Issue.id).desc())
        .all()
    )

    unassigned_rows = (
        db.query(models.Issue.status, func.count(models.Issue.id))
        .filter(models.Issue.department_id.is_(None))
        .filter(*historical)
        .group_by(models.Issue.status)
        .all()
    )
    unassigned_statuses = {
        status.value: count for status, count in unassigned_rows
    }

    return {
        "city_scope": "Bengaluru, Karnataka, India",
        "departments": [
            {
                "name": row.name,
                "source_agency": row.source_agency or row.name,
                "city": row.city,
                "records": int(row.records or 0),
                "reported": int(row.reported or 0),
                "in_progress": int(row.in_progress or 0),
                "resolved": int(row.resolved or 0),
                "rejected": int(row.rejected or 0),
            }
            for row in rows
        ],
        "unassigned_records": sum(unassigned_statuses.values()),
        "unassigned_status_counts": unassigned_statuses,
        "note": "Agency assignments are based on the agency recorded in the source dataset.",
    }


@router.get("/heatmap")
def heatmap_points(
    date_from: Optional[str] = Query(default=None, max_length=40),
    date_to: Optional[str] = Query(default=None, max_length=40),
    db: Session = Depends(get_db),
):
    """Return recorded historical open-issue coordinates.

    Live citizen submissions are excluded so public endpoints never disclose
    their precise reporting locations. Priority weights are omitted when the
    source does not provide them.
    """
    issues = (
        db.query(models.Issue)
        .filter(*_historical_filters(date_from=date_from, date_to=date_to))
        .filter(models.Issue.status.notin_([
            models.IssueStatus.RESOLVED,
            models.IssueStatus.REJECTED,
            models.IssueStatus.DUPLICATE,
        ]))
        .all()
    )
    result = []
    for issue in issues:
        point = {"lat": issue.latitude, "lng": issue.longitude}
        if issue.priority_score is not None:
            point["weight"] = float(issue.priority_score)
        result.append(point)
    return result
