"""Baseline schema captured before Task 6 explainability columns.

This revision represents the application schema exactly as it existed before
the AI-explainability columns were introduced. It exists so that:

* a brand-new empty database can be created from scratch with Alembic, and
* an existing pre-Alembic database can be *stamped* at this revision instead of
  being recreated or migrated twice.

The revision is intentionally written with literal enum values rather than
importing the live application models, so it stays reproducible even if the
models evolve later.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-13

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


# Literal copies of the enum values in use at baseline time.
ISSUE_CATEGORY_VALUES = (
    "Road_Damage",
    "Garbage_Waste",
    "Streetlight",
    "Water_Supply",
    "Drainage_Sewage",
    "Traffic_Signal",
    "Traffic_Management",
    "Trees_Environment",
    "Illegal_Construction",
    "Public_Property_Damage",
    "Other",
)

ISSUE_STATUS_VALUES = (
    "reported",
    "acknowledged",
    "in_progress",
    "resolved",
    "rejected",
    "duplicate",
)

PRIORITY_LEVEL_VALUES = ("Low", "Medium", "High", "Critical")
USER_ROLE_VALUES = ("citizen", "officer", "admin")


def _enum(values: tuple[str, ...], name: str) -> sa.Enum:
    # ``native_enum=False`` renders VARCHAR(n) on SQLite (matching the existing
    # database) and a portable VARCHAR on other backends. No CHECK constraint is
    # created, matching the schema produced by SQLAlchemy's create_all().
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=False)


def upgrade() -> None:
    op.create_table(
        "departments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "handles_category",
            _enum(ISSUE_CATEGORY_VALUES, "issuecategory"),
            nullable=False,
        ),
        sa.Column("contact_email", sa.String(length=150), nullable=True),
        sa.Column("contact_phone", sa.String(length=20), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("source_agency", sa.String(length=150), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=150), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", _enum(USER_ROLE_VALUES, "userrole"), nullable=False),
        sa.Column("department_id", sa.String(length=36), nullable=True),
        sa.Column("preferred_language", sa.String(length=10), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("phone"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "issues",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("ticket_number", sa.String(length=30), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "category", _enum(ISSUE_CATEGORY_VALUES, "issuecategory"), nullable=False
        ),
        sa.Column("category_confidence", sa.Float(), nullable=True),
        sa.Column("category_source", sa.String(length=20), nullable=True),
        sa.Column("priority_score", sa.Float(), nullable=True),
        sa.Column(
            "priority_level", _enum(PRIORITY_LEVEL_VALUES, "prioritylevel"), nullable=True
        ),
        sa.Column(
            "status", _enum(ISSUE_STATUS_VALUES, "issuestatus"), nullable=False
        ),
        sa.Column("location", sa.String(length=100), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("address_text", sa.String(length=300), nullable=True),
        sa.Column("duplicate_of_issue_id", sa.String(length=36), nullable=True),
        sa.Column("duplicate_count", sa.Integer(), nullable=True),
        sa.Column("hotspot_cluster_id", sa.Integer(), nullable=True),
        sa.Column("reporter_id", sa.String(length=36), nullable=False),
        sa.Column("department_id", sa.String(length=36), nullable=True),
        sa.Column("assigned_officer_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("source_dataset", sa.String(length=200), nullable=True),
        sa.Column("source_record_id", sa.String(length=80), nullable=True),
        sa.Column("source_status", sa.String(length=40), nullable=True),
        sa.Column("source_agency", sa.String(length=150), nullable=True),
        sa.Column("source_category", sa.String(length=150), nullable=True),
        sa.Column("source_subcategory", sa.String(length=200), nullable=True),
        sa.Column("is_historical", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["duplicate_of_issue_id"], ["issues.id"]
        ),
        sa.ForeignKeyConstraint(["reporter_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"]),
        sa.ForeignKeyConstraint(["assigned_officer_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_issues_ticket_number", "issues", ["ticket_number"], unique=True
    )
    op.create_index(
        "ix_issues_source_record_id", "issues", ["source_record_id"], unique=False
    )

    op.create_table(
        "issue_images",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("issue_id", sa.String(length=36), nullable=False),
        sa.Column("image_url", sa.String(length=500), nullable=False),
        sa.Column("blur_score", sa.Float(), nullable=True),
        sa.Column("brightness", sa.Float(), nullable=True),
        sa.Column("resolution_width", sa.Integer(), nullable=True),
        sa.Column("resolution_height", sa.Integer(), nullable=True),
        sa.Column("passed_verification", sa.Boolean(), nullable=True),
        sa.Column("perceptual_hash", sa.String(length=80), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "issue_status_updates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("issue_id", sa.String(length=36), nullable=False),
        sa.Column("updated_by_id", sa.String(length=36), nullable=True),
        sa.Column("old_status", _enum(ISSUE_STATUS_VALUES, "issuestatus"), nullable=True),
        sa.Column("new_status", _enum(ISSUE_STATUS_VALUES, "issuestatus"), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"]),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("issue_status_updates")
    op.drop_table("issue_images")
    op.drop_index("ix_issues_source_record_id", table_name="issues")
    op.drop_index("ix_issues_ticket_number", table_name="issues")
    op.drop_table("issues")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    op.drop_table("departments")
