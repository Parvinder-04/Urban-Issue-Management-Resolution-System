"""Administrative audit helpers.

The audit log is append-only. This module only *adds* entries; it never updates
or deletes them. Callers must add the business change and the audit entry to the
same SQLAlchemy session and commit once, so a failed action cannot leave a
misleading success event behind.

Payloads are deliberately limited to identifiers, routing/status values and
administrative display names. Passwords, password hashes, tokens, raw auth
headers and citizen contact details are never recorded.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app import models

# Stable action identifiers used by the portal and its tests.
ACTION_ISSUE_ROUTED = "issue.routed"
ACTION_ISSUE_LEFT_UNASSIGNED = "issue.left_unassigned"
ACTION_OFFICER_CREATED = "officer.created"
ACTION_OFFICER_DEPARTMENT_CHANGED = "officer.department_changed"

AUDIT_ACTIONS = (
    ACTION_ISSUE_ROUTED,
    ACTION_ISSUE_LEFT_UNASSIGNED,
    ACTION_OFFICER_CREATED,
    ACTION_OFFICER_DEPARTMENT_CHANGED,
)

ENTITY_ISSUE = "issue"
ENTITY_USER = "user"

ENTITY_TYPES = (ENTITY_ISSUE, ENTITY_USER)


def issue_routing_state(issue: models.Issue) -> dict[str, Any]:
    """Return a safe snapshot of an issue's routing/ownership state."""
    return {
        "issue_id": issue.id,
        "status": issue.status.value if issue.status else None,
        "department_id": issue.department_id,
        "routing_status": issue.routing_status,
        "routing_reason": issue.routing_reason,
        "assigned_officer_id": issue.assigned_officer_id,
        "city": issue.city,
        "category": (
            issue.category.value if issue.category is not None else None
        ),
    }


def officer_department_state(user: models.User) -> dict[str, Any]:
    """Return a safe snapshot of an officer's role/department assignment."""
    return {
        "user_id": user.id,
        "role": user.role.value if user.role else None,
        "department_id": user.department_id,
    }


def record_admin_audit(
    db: Session,
    *,
    actor: models.User,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    reason: str | None = None,
    city: str | None = None,
) -> models.AdminAuditLog:
    """Append one audit entry to the caller's transaction.

    The entry is flushed so its id is available to the response, but the caller
    owns the commit. If the caller rolls back, the audit row disappears with the
    failed business change.
    """
    entry = models.AdminAuditLog(
        actor_user_id=actor.id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_state=before_state,
        after_state=after_state,
        reason=(reason or "").strip() or None,
        city=city,
    )
    db.add(entry)
    db.flush()
    return entry
