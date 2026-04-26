"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-25

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("google_id", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("picture_url", sa.String(length=1024), nullable=True),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=False),
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
        sa.UniqueConstraint("google_id", name="uq_users_google_id"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_google_id", "users", ["google_id"])
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "syllabi",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("course_name", sa.String(length=512), nullable=True),
        sa.Column("course_code", sa.String(length=128), nullable=True),
        sa.Column("term", sa.String(length=128), nullable=True),
        sa.Column("parsed_events", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
    op.create_index("ix_syllabi_user_id", "syllabi", ["user_id"])

    event_type = postgresql.ENUM(
        "assignment",
        "exam",
        "lecture",
        "holiday",
        "office_hours",
        "other",
        name="event_type",
    )
    event_type.create(op.get_bind(), checkfirst=True)
    confidence_level = postgresql.ENUM(
        "high", "medium", "low", name="confidence_level"
    )
    confidence_level.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "syllabus_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("syllabi.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("google_calendar_id", sa.String(length=512), nullable=True),
        sa.Column("google_event_id", sa.String(length=512), nullable=True),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("start_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_all_day", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("recurrence_rule", sa.String(length=1024), nullable=True),
        sa.Column(
            "event_type",
            sa.Enum(
                "assignment",
                "exam",
                "lecture",
                "holiday",
                "office_hours",
                "other",
                name="event_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "confidence",
            sa.Enum(
                "high", "medium", "low", name="confidence_level", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint(
            "google_calendar_id",
            "google_event_id",
            name="uq_events_google_calendar_event",
        ),
    )
    op.create_index("ix_events_syllabus_id", "events", ["syllabus_id"])
    op.create_index("ix_events_user_id", "events", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_events_user_id", table_name="events")
    op.drop_index("ix_events_syllabus_id", table_name="events")
    op.drop_table("events")
    op.execute("DROP TYPE IF EXISTS confidence_level")
    op.execute("DROP TYPE IF EXISTS event_type")

    op.drop_index("ix_syllabi_user_id", table_name="syllabi")
    op.drop_table("syllabi")

    op.drop_index("ix_users_email", table_name="users")
    op.drop_index("ix_users_google_id", table_name="users")
    op.drop_table("users")
