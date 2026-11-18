"""Persist AI classification and priority explainability.

Adds six nullable columns to ``issues``. Every column is nullable so existing
historical rows remain valid and are never back-filled or fabricated.

Revision ID: 0002_ai_explainability
Revises: 0001_baseline
Create Date: 2026-11-13

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0002_ai_explainability"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


EXPLAINABILITY_COLUMN_NAMES = (
    "category_model_version",
    "category_needs_review",
    "category_review_reason",
    "category_top_alternatives",
    "category_score_kind",
    "priority_reasons",
)


def _explainability_columns() -> list[sa.Column]:
    """Fresh Column objects, so repeated runs never share table state."""
    return [
        sa.Column("category_model_version", sa.String(length=80), nullable=True),
        sa.Column("category_needs_review", sa.Boolean(), nullable=True),
        sa.Column("category_review_reason", sa.Text(), nullable=True),
        sa.Column("category_top_alternatives", sa.JSON(), nullable=True),
        sa.Column("category_score_kind", sa.String(length=40), nullable=True),
        sa.Column("priority_reasons", sa.JSON(), nullable=True),
    ]


def upgrade() -> None:
    # ADD COLUMN is a native, non-destructive operation on SQLite. Batch mode is
    # still used so the same revision works on backends that require a table
    # rebuild for this operation; Alembic keeps it as a plain ALTER when it can.
    with op.batch_alter_table("issues") as batch_op:
        for column in _explainability_columns():
            batch_op.add_column(column)


def downgrade() -> None:
    # Downgrade is only intended for disposable databases. Batch mode rebuilds
    # the SQLite table so the columns can be removed without losing other data.
    with op.batch_alter_table("issues") as batch_op:
        for name in reversed(EXPLAINABILITY_COLUMN_NAMES):
            batch_op.drop_column(name)
