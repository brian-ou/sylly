"""study tool: concepts + attempts for active recall

Revision ID: 0005_study_tools
Revises: 0004_recurring_overrides
Create Date: 2026-05-01

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005_study_tools"
down_revision: Union[str, None] = "0004_recurring_overrides"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Defensive cleanup: mirror 0001_initial style so reruns are safe.
    op.execute("DROP TABLE IF EXISTS study_attempts CASCADE")
    op.execute("DROP TABLE IF EXISTS study_concepts CASCADE")

    op.create_table(
        "study_concepts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "syllabus_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("syllabi.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=True),
        sa.Column(
            "mastery_score",
            sa.Numeric(precision=4, scale=3),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "times_seen", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "times_correct",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "last_attempted_at", sa.DateTime(timezone=True), nullable=True
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
    op.create_index("ix_study_concepts_user_id", "study_concepts", ["user_id"])
    op.create_index(
        "ix_study_concepts_syllabus_id", "study_concepts", ["syllabus_id"]
    )

    op.create_table(
        "study_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "concept_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("study_concepts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("user_answer", sa.Text(), nullable=False),
        sa.Column("correct", sa.Boolean(), nullable=False),
        sa.Column(
            "score", sa.Numeric(precision=4, scale=3), nullable=False
        ),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_study_attempts_user_id", "study_attempts", ["user_id"])
    op.create_index(
        "ix_study_attempts_concept_id", "study_attempts", ["concept_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_study_attempts_concept_id", table_name="study_attempts")
    op.drop_index("ix_study_attempts_user_id", table_name="study_attempts")
    op.drop_table("study_attempts")
    op.drop_index("ix_study_concepts_syllabus_id", table_name="study_concepts")
    op.drop_index("ix_study_concepts_user_id", table_name="study_concepts")
    op.drop_table("study_concepts")
