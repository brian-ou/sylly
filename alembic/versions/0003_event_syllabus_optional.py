"""make events.syllabus_id nullable

Revision ID: 0003_event_syllabus_optional
Revises: 0002_grade_categories
Create Date: 2026-04-26

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_event_syllabus_optional"
down_revision: Union[str, None] = "0002_grade_categories"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Standalone events (created via the chat agent or POST /events without a
    # syllabus) need to exist, so syllabus_id becomes optional.
    op.alter_column("events", "syllabus_id", nullable=True)


def downgrade() -> None:
    op.alter_column("events", "syllabus_id", nullable=False)
