"""Immutable administrative audit log.

Adds the ``admin_audit_logs`` table used by the administrator portal. The
migration is additive: no existing table, row or column is rewritten. Existing
``issue_status_updates`` records remain untouched; the general audit log
complements them.

Revision ID: 0004_admin_audit_log
Revises: 0003_department_routing
Create Date: 2026-11-14

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0004_admin_audit_log"
down_revision = "0003_department_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=True),
        sa.Column("before_state", sa.JSON(), nullable=True),
        sa.Column("after_state", sa.JSON(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_audit_logs_actor_user_id",
        "admin_audit_logs",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_admin_audit_logs_action", "admin_audit_logs", ["action"], unique=False
    )
    op.create_index(
        "ix_admin_audit_logs_entity_type",
        "admin_audit_logs",
        ["entity_type"],
        unique=False,
    )
    op.create_index(
        "ix_admin_audit_logs_entity_id",
        "admin_audit_logs",
        ["entity_id"],
        unique=False,
    )
    op.create_index(
        "ix_admin_audit_logs_city", "admin_audit_logs", ["city"], unique=False
    )
    op.create_index(
        "ix_admin_audit_logs_created_at",
        "admin_audit_logs",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    # Downgrade is only intended for disposable databases. Audit history is
    # never removed from a production database.
    op.drop_index("ix_admin_audit_logs_created_at", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_city", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_entity_id", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_entity_type", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_action", table_name="admin_audit_logs")
    op.drop_index(
        "ix_admin_audit_logs_actor_user_id", table_name="admin_audit_logs"
    )
    op.drop_table("admin_audit_logs")
