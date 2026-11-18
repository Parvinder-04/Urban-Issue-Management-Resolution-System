"""Verified department directory and city-aware routing outcome.

Adds verified-directory metadata to ``departments`` and a routing outcome to
``issues``. Every new department column is nullable or carries a safe server
default, so existing rows (including the historical Bengaluru directory) remain
valid and are never re-written or re-assigned by this migration.

Revision ID: 0003_department_routing
Revises: 0002_ai_explainability
Create Date: 2026-11-14

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0003_department_routing"
down_revision = "0002_ai_explainability"
branch_labels = None
depends_on = None


DEPARTMENT_ROUTING_COLUMN_NAMES = (
    "short_name",
    "jurisdiction",
    "contact_purpose",
    "verification_source_url",
    "verification_date",
    "routing_mode",
    "is_active",
)

ISSUE_ROUTING_COLUMN_NAMES = (
    "routing_status",
    "routing_reason",
)


def _department_columns() -> list[sa.Column]:
    """Fresh Column objects, so repeated runs never share table state."""
    return [
        sa.Column("short_name", sa.String(length=60), nullable=True),
        sa.Column("jurisdiction", sa.String(length=300), nullable=True),
        sa.Column("contact_purpose", sa.String(length=200), nullable=True),
        sa.Column("verification_source_url", sa.String(length=500), nullable=True),
        sa.Column("verification_date", sa.String(length=10), nullable=True),
        # Existing directory rows are deliberately not trusted for automatic
        # assignment; a reviewed data update must opt them in explicitly.
        sa.Column(
            "routing_mode",
            sa.String(length=20),
            nullable=False,
            server_default="manual_review",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default="1",
        ),
    ]


def _issue_columns() -> list[sa.Column]:
    """Fresh Column objects, so repeated runs never share table state."""
    return [
        # NULL on historical rows: they are never re-routed.
        sa.Column("routing_status", sa.String(length=30), nullable=True),
        sa.Column("routing_reason", sa.Text(), nullable=True),
    ]


def upgrade() -> None:
    # ADD COLUMN is a native, non-destructive operation on SQLite. Batch mode is
    # still used so the same revision works on backends that require a rebuild.
    with op.batch_alter_table("departments") as batch_op:
        for column in _department_columns():
            batch_op.add_column(column)

    with op.batch_alter_table("issues") as batch_op:
        for column in _issue_columns():
            batch_op.add_column(column)


def downgrade() -> None:
    # Downgrade is only intended for disposable databases. Batch mode rebuilds
    # the SQLite tables so the columns can be removed without losing other data.
    with op.batch_alter_table("issues") as batch_op:
        for name in reversed(ISSUE_ROUTING_COLUMN_NAMES):
            batch_op.drop_column(name)

    with op.batch_alter_table("departments") as batch_op:
        for name in reversed(DEPARTMENT_ROUTING_COLUMN_NAMES):
            batch_op.drop_column(name)
