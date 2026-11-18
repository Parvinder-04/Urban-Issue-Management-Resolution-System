"""Pydantic schemas — API request/response contracts."""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.models import IssueCategory, IssueStatus, PriorityLevel, UserRole


# ---------- Users ----------
class UserCreate(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    password: str = Field(min_length=8)
    preferred_language: str = "en"

    @field_validator("password")
    @classmethod
    def validate_password_length(cls, v: str) -> str:
        if len(v.encode("utf-8")) > 72:
            raise ValueError("Password cannot exceed 72 bytes")
        return v


class UserOut(BaseModel):
    id: str
    name: str
    email: EmailStr
    phone: Optional[str] = None
    role: UserRole
    department_id: Optional[str] = None
    preferred_language: str = "en"
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


# ---------- Issues ----------
class IssueCreate(BaseModel):
    title: str = Field(min_length=5, max_length=200)
    description: str = Field(min_length=10)
    city: str = Field(default="Gurugram, Haryana, India", min_length=2, max_length=120)
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    address_text: Optional[str] = None

    @model_validator(mode="after")
    def validate_city_and_coordinates(self) -> "IssueCreate":
        c_lower = (self.city or "").lower().strip()
        if "gurugram" in c_lower or "gurgaon" in c_lower:
            self.city = "Gurugram, Haryana, India"
            if not (28.20 <= self.latitude <= 28.65 and 76.75 <= self.longitude <= 77.35):
                raise ValueError(
                    f"Coordinates ({self.latitude}, {self.longitude}) are outside the municipal boundary of Gurugram."
                )
        elif "bengaluru" in c_lower or "bangalore" in c_lower:
            self.city = "Bengaluru, Karnataka, India"
            if not (12.60 <= self.latitude <= 13.35 and 77.25 <= self.longitude <= 77.95):
                raise ValueError(
                    f"Coordinates ({self.latitude}, {self.longitude}) are outside the municipal boundary of Bengaluru."
                )
        else:
            raise ValueError(
                f"Unsupported city '{self.city}'. Supported operating cities are Gurugram and Bengaluru."
            )
        return self


class IssueOut(BaseModel):
    id: str
    ticket_number: Optional[str] = None
    title: str
    description: str
    category: IssueCategory
    category_confidence: Optional[float] = None
    priority_score: Optional[float] = None
    priority_level: Optional[PriorityLevel] = None
    status: IssueStatus
    latitude: float
    longitude: float
    address_text: Optional[str] = None
    duplicate_count: Optional[int] = None
    reporter_id: Optional[str] = None
    department_id: Optional[str] = None
    assigned_officer_id: Optional[str] = None
    routing_status: Optional[str] = None
    routing_reason: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    city: Optional[str] = None
    source_dataset: Optional[str] = None
    source_record_id: Optional[str] = None
    source_status: Optional[str] = None
    source_agency: Optional[str] = None
    source_category: Optional[str] = None
    source_subcategory: Optional[str] = None
    category_source: Optional[str] = None
    is_historical: bool = False

    # AI explainability. These are exposed only through authenticated issue
    # responses (create/list/detail/status). They are deliberately absent from
    # the public historical dashboard payload. NULL means "not available",
    # never a fabricated value.
    category_model_version: Optional[str] = None
    category_needs_review: Optional[bool] = None
    category_review_reason: Optional[str] = None
    category_top_alternatives: Optional[list[dict]] = None
    category_score_kind: Optional[str] = None
    priority_reasons: Optional[list[str]] = None

    @model_validator(mode="after")
    def suppress_ticket_number_for_historical(self) -> "IssueOut":
        if self.is_historical:
            self.ticket_number = None
        return self

    class Config:
        from_attributes = True


class IssueStatusUpdateCreate(BaseModel):
    new_status: IssueStatus
    comment: Optional[str] = None


class IssueStatusUpdateOut(BaseModel):
    id: str
    issue_id: str
    updated_by_id: Optional[str] = None
    old_status: Optional[IssueStatus] = None
    new_status: IssueStatus
    comment: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class IssueImageOut(BaseModel):
    id: str
    image_url: str
    passed_verification: Optional[bool] = None
    blur_score: Optional[float] = None
    brightness: Optional[float] = None
    resolution_width: Optional[int] = None
    resolution_height: Optional[int] = None
    uploaded_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ---------- Officer portal ----------
class OfficerDepartmentOut(BaseModel):
    id: str
    name: str
    short_name: Optional[str] = None
    city: Optional[str] = None
    handles_category: Optional[str] = None
    routing_mode: Optional[str] = None

    class Config:
        from_attributes = True


class OfficerOverviewCounts(BaseModel):
    """Operational counts, all derived from live department-scoped queries.

    ``awaiting_review`` is deliberately ``None`` unless the officer is
    explicitly authorised to see manual-review queues. Task 9 does not grant
    that permission, so the portal never displays a fabricated figure.
    """

    total_live_routed: int
    new_reported: int
    unclaimed: int
    assigned_to_me: int
    my_open: int
    in_progress: int
    resolved: int
    awaiting_review: Optional[int] = None
    awaiting_review_visible: bool = False


class OfficerOverviewOut(BaseModel):
    department: OfficerDepartmentOut
    counts: OfficerOverviewCounts
    resolution_timing_available: bool = False
    note: Optional[str] = None


class OfficerIssuePermissions(BaseModel):
    can_claim: bool
    can_update: bool
    is_assigned_to_me: bool
    assigned_to_another_officer: bool


class OfficerIssueDetailOut(BaseModel):
    issue: IssueOut
    department: OfficerDepartmentOut
    images: list[IssueImageOut]
    history: list[IssueStatusUpdateOut]
    permissions: OfficerIssuePermissions


class OfficerClaimResponse(BaseModel):
    issue: IssueOut
    claimed: bool
    already_claimed_by_you: bool = False
    message: str


# ---------- Administrator portal ----------
class AdminDepartmentOut(BaseModel):
    """Verified directory row exposed to administrators (read-only)."""

    id: str
    name: str
    short_name: Optional[str] = None
    source_agency: Optional[str] = None
    handles_category: Optional[str] = None
    city: Optional[str] = None
    jurisdiction: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_purpose: Optional[str] = None
    verification_source_url: Optional[str] = None
    verification_date: Optional[str] = None
    routing_mode: Optional[str] = None
    is_active: bool = True

    class Config:
        from_attributes = True


class AdminRoutingCandidateOut(AdminDepartmentOut):
    """A same-city/same-category directory row and whether it is selectable."""

    selectable: bool
    blocked_reasons: list[str] = []


class AdminIssueOut(IssueOut):
    """Issue response with administrative display names.

    Reporter contact details are never included. ``reporter_name`` is populated
    only on the single-issue detail view.
    """

    department_name: Optional[str] = None
    assigned_officer_name: Optional[str] = None
    reporter_name: Optional[str] = None


class AdminOfficerSummaryOut(BaseModel):
    id: str
    name: str
    role: UserRole
    department_id: Optional[str] = None


class AdminIssueDetailOut(BaseModel):
    issue: AdminIssueOut
    department: Optional[AdminDepartmentOut] = None
    reporter_name: Optional[str] = None
    assigned_officer: Optional[AdminOfficerSummaryOut] = None
    images: list[IssueImageOut]
    history: list[IssueStatusUpdateOut]
    candidates: list[AdminRoutingCandidateOut]
    can_route: bool
    routing_block_reason: Optional[str] = None


class AdminLiveStatusCounts(BaseModel):
    reported: int = 0
    acknowledged: int = 0
    in_progress: int = 0
    resolved: int = 0
    rejected: int = 0
    duplicate: int = 0


class AdminDepartmentWorkloadOut(BaseModel):
    department_id: str
    name: str
    city: Optional[str] = None
    handles_category: Optional[str] = None
    count: int


class AdminCityCountOut(BaseModel):
    city: Optional[str] = None
    count: int


class AdminHistoricalOverviewOut(BaseModel):
    total: int
    source_agency_distribution: list[dict]
    category_distribution: list[dict]
    note: str


class AdminLiveOverviewOut(BaseModel):
    total: int
    statuses: AdminLiveStatusCounts
    assigned: int
    unassigned: int
    awaiting_review: int
    needs_ai_review: int
    department_workload: list[AdminDepartmentWorkloadOut]
    city_distribution: list[AdminCityCountOut]


class AdminOverviewFiltersOut(BaseModel):
    city: Optional[str] = None
    department_id: Optional[str] = None


class AdminOverviewOut(BaseModel):
    filters: AdminOverviewFiltersOut
    live: AdminLiveOverviewOut
    historical: AdminHistoricalOverviewOut
    resolution_timing_available: bool = False
    notes: list[str] = []


class AdminRouteRequest(BaseModel):
    """Route to a verified department, or leave unassigned when omitted."""

    department_id: Optional[str] = None
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def validate_meaningful_reason(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if len(cleaned) < 10:
            raise ValueError(
                "A meaningful administrative justification of at least 10 "
                "characters is required."
            )
        return cleaned


class AdminRoutingResponse(BaseModel):
    issue: AdminIssueOut
    action: str
    audit_id: str
    message: str


class AdminOfficerCreate(UserCreate):
    department_id: str = Field(min_length=1, max_length=36)


class AdminOfficerDepartmentUpdate(BaseModel):
    department_id: str = Field(min_length=1, max_length=36)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def validate_meaningful_reason(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if len(cleaned) < 10:
            raise ValueError(
                "A meaningful justification of at least 10 characters is required."
            )
        return cleaned


class AdminOfficerOut(BaseModel):
    id: str
    name: str
    email: EmailStr
    phone: Optional[str] = None
    role: UserRole
    department_id: Optional[str] = None
    department_name: Optional[str] = None
    department_city: Optional[str] = None
    preferred_language: str = "en"
    created_at: Optional[datetime] = None


class AdminAuditLogOut(BaseModel):
    id: str
    actor_user_id: str
    actor_name: Optional[str] = None
    action: str
    entity_type: str
    entity_id: Optional[str] = None
    before_state: Optional[dict] = None
    after_state: Optional[dict] = None
    reason: Optional[str] = None
    city: Optional[str] = None
    created_at: datetime


class AdminAuditLogPageOut(BaseModel):
    items: list[AdminAuditLogOut]
    total: int
    skip: int
    limit: int


# ---------- Analytics ----------
class CategoryBreakdown(BaseModel):
    category: str
    count: int
    avg_resolution_hours: Optional[float]


class HotspotOut(BaseModel):
    cluster_id: int
    center_lat: float
    center_lon: float
    issue_count: int
    categories: list[str]


class DashboardSummary(BaseModel):
    total_issues: int
    open_issues: int
    resolved_this_week: Optional[int] = None
    avg_resolution_hours: Optional[float] = None
    category_breakdown: list[CategoryBreakdown]
    hotspots: list[HotspotOut]


# ---------- Chatbot ----------
class ChatMessageIn(BaseModel):
    message: str
    user_id: Optional[str] = None


class ChatMessageOut(BaseModel):
    intent: str
    response: str
    needs_human_handoff: bool
