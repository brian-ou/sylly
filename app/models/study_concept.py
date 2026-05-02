"""StudyConcept ORM model: a single learnable item the student is practicing."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.study_attempt import StudyAttempt
    from app.models.syllabus import Syllabus
    from app.models.user import User


class StudyConcept(Base):
    """A discrete concept extracted from course material.

    Mastery is a rolling 0..1 score updated after each grading attempt — see
    `app/services/study_agent.py` for the update rule. `times_seen` and
    `times_correct` are cheap denormalized counters to keep the progress view
    fast without aggregating attempts on every read.
    """

    __tablename__ = "study_concepts"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # SET NULL on syllabus delete: pasted-in study material can outlive the
    # syllabus that originally inspired it.
    syllabus_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("syllabi.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    mastery_score: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, default=Decimal("0")
    )
    times_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    times_correct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    user: Mapped["User"] = relationship("User", back_populates="study_concepts")
    syllabus: Mapped[Optional["Syllabus"]] = relationship(
        "Syllabus", back_populates="study_concepts"
    )
    attempts: Mapped[List["StudyAttempt"]] = relationship(
        "StudyAttempt",
        back_populates="concept",
        cascade="all, delete-orphan",
        order_by="StudyAttempt.attempted_at.desc()",
    )
