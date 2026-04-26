"""grade_categories table

Revision ID: 0002_grade_categories
Revises: 0001_initial
Create Date: 2026-04-26

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_grade_categories"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Defensive cleanup: if a previous failed run left partial state behind,
    # remove it so we can start cleanly. Mirrors the style of 0001_initial so
    # reruns are safe.
    op.execute("DROP TABLE IF EXISTS grade_categories CASCADE")

    op.create_table(
        "grade_categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "syllabus_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("syllabi.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("weight", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column(
            "drop_lowest",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "sort_order",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_grade_categories_syllabus_id",
        "grade_categories",
        ["syllabus_id"],
    )
    # Case-insensitive unique constraint per syllabus, declared as a
    # functional unique index since Postgres can't enforce uniqueness on
    # lower(name) via a plain UNIQUE constraint.
    op.create_index(
        "uq_grade_categories_syllabus_lower_name",
        "grade_categories",
        ["syllabus_id", sa.text("lower(name)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_grade_categories_syllabus_lower_name", table_name="grade_categories"
    )
    op.drop_index("ix_grade_categories_syllabus_id", table_name="grade_categories")
    op.drop_table("grade_categories")
