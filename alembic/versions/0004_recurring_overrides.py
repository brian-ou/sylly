"""recurring event overrides: exdates + per-instance override link

Revision ID: 0004_recurring_overrides
Revises: 0003_event_syllabus_optional
Create Date: 2026-04-26

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004_recurring_overrides"
down_revision: Union[str, None] = "0003_event_syllabus_optional"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column(
            "exdates",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "events",
        sa.Column(
            "override_of_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.add_column(
        "events",
        sa.Column(
            "override_original_start",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_events_override_of_id", "events", ["override_of_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_events_override_of_id", table_name="events")
    op.drop_column("events", "override_original_start")
    op.drop_column("events", "override_of_id")
    op.drop_column("events", "exdates")
