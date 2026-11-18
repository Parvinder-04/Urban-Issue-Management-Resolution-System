"""City-aware department routing for citizen complaints.

This module is the only place that decides which department a newly created
live issue is assigned to. The rules are deliberately conservative:

* The department must match the issue's canonical city *exactly*. There is no
  category-only fallback, so a Gurugram complaint can never reach a Bengaluru
  department (or the reverse).
* Automatic assignment is allowed only when the directory row is active,
  ``routing_mode == "automatic"``, and the row carries a verification source
  URL and verification date. An unverified row is downgraded to manual review
  instead of being trusted.
* ``manual_review`` mappings keep ``department_id`` NULL: an officer is never
  silently assigned when ownership or jurisdiction is uncertain.
* A missing or disabled mapping produces an explicitly unassigned issue.

The decision is returned as a :class:`RoutingDecision`; the caller persists the
status and reason verbatim.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app import models

ROUTING_MODE_AUTOMATIC = "automatic"
ROUTING_MODE_MANUAL_REVIEW = "manual_review"
ROUTING_MODE_DISABLED = "disabled"

ROUTING_MODES = (
    ROUTING_MODE_AUTOMATIC,
    ROUTING_MODE_MANUAL_REVIEW,
    ROUTING_MODE_DISABLED,
)

ROUTING_STATUS_ASSIGNED = "assigned"
ROUTING_STATUS_UNASSIGNED_REVIEW = "unassigned_review"
ROUTING_STATUS_UNASSIGNED_NO_MAPPING = "unassigned_no_mapping"

ROUTING_STATUSES = (
    ROUTING_STATUS_ASSIGNED,
    ROUTING_STATUS_UNASSIGNED_REVIEW,
    ROUTING_STATUS_UNASSIGNED_NO_MAPPING,
)


@dataclass(frozen=True)
class RoutingDecision:
    """Outcome of a routing lookup.

    ``department`` is set only for a verified automatic assignment. Every other
    outcome leaves it ``None`` so no officer queue receives the complaint.
    """

    department: models.Department | None
    status: str
    reason: str


def _unassigned_no_mapping(reason: str) -> RoutingDecision:
    return RoutingDecision(None, ROUTING_STATUS_UNASSIGNED_NO_MAPPING, reason)


def _unassigned_review(reason: str) -> RoutingDecision:
    return RoutingDecision(None, ROUTING_STATUS_UNASSIGNED_REVIEW, reason)


def resolve_department(
    db: Session, city: str | None, category: models.IssueCategory
) -> RoutingDecision:
    """Resolve the verified department for ``category`` in ``city``.

    The lookup is strictly scoped to one city and one category. It never falls
    back to another city, and it never assigns a department that has not been
    verified for automatic routing.
    """
    if not city or not str(city).strip():
        return _unassigned_no_mapping(
            "No canonical city was supplied, so no city-specific department "
            "could be verified."
        )

    city_value = str(city).strip()

    department = (
        db.query(models.Department)
        .filter(
            models.Department.handles_category == category,
            models.Department.city == city_value,
            models.Department.is_active.is_(True),
        )
        .order_by(models.Department.id)
        .first()
    )

    if department is None:
        return _unassigned_no_mapping(
            f"No active verified department mapping exists for category "
            f"'{category.value}' in '{city_value}'."
        )

    mode = department.routing_mode or ROUTING_MODE_MANUAL_REVIEW

    if mode == ROUTING_MODE_AUTOMATIC:
        if not department.verification_source_url or not department.verification_date:
            return _unassigned_review(
                f"Department '{department.name}' is marked automatic but has no "
                "verification source/date, so it cannot be auto-assigned. "
                "Manual review is required."
            )
        return RoutingDecision(
            department,
            ROUTING_STATUS_ASSIGNED,
            f"Assigned automatically to '{department.name}' for category "
            f"'{category.value}' in '{city_value}' using verified mapping "
            f"{department.verification_source_url} (checked "
            f"{department.verification_date}).",
        )

    if mode == ROUTING_MODE_DISABLED:
        return _unassigned_no_mapping(
            f"The mapping for category '{category.value}' in '{city_value}' is "
            f"disabled and is not approved for assignment."
        )

    # manual_review (including any unknown mode: fail safe).
    return _unassigned_review(
        f"Category '{category.value}' in '{city_value}' requires jurisdiction "
        f"review. Candidate authority '{department.name}' is not automatically "
        "assigned because asset ownership or the responsible agency is not "
        "verified for every location."
    )
