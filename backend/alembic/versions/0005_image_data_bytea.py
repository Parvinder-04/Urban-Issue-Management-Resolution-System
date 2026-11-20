"""Image storage in the database.

Adds ``image_data`` (BYTEA/BLOB) to ``issue_images`` so evidence photographs can
be stored inside the application database instead of on a local filesystem.
This is required for free-tier deployments that have no persistent disk.

The migration is additive: ``image_url`` keeps its existing meaning for
filesystem-backed rows, and ``image_data`` is nullable so older rows remain
valid. No existing row is rewritten.

Revision ID: 0005_image_data_bytea
Revises: 0004_admin_audit_log
Create Date: 2026-11-20

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_image_data_bytea"
down_revision = "0004_admin_audit_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("issue_images") as batch_op:
        batch_op.add_column(sa.Column("image_data", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("issue_images") as batch_op:
        batch_op.drop_column("image_data")
