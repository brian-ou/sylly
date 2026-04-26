"""Event ORM model."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.syllabus import Syllabus
    from app.models.user import User


class EventType(str, enum.Enum):
    ASSIGNMENT = "assignment"
    EXAM = "exam"
    LECTURE = "lecture"
    HOLIDAY = "holiday"
    OFFICE_HOURS = "office_hours"
    OTHER = "other"


class ConfidenceLevel(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Event(Base):
    """A single dated event extracted from a syllabus."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint(
            "google_calendar_id",
            "google_event_id",
            name="uq_events_google_calendar_event",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    syllabus_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("syllabi.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    google_calendar_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    google_event_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_datetime: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    end_datetime: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recurrence_rule: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    event_type: Mapped[EventType] = mapped_column(
        SAEnum(EventType, name="event_type"), nullable=False, default=EventType.OTHER
    )
    confidence: Mapped[ConfidenceLevel] = mapped_column(
        SAEnum(ConfidenceLevel, name="confidence_level"),
        nullable=False,
        default=ConfidenceLevel.MEDIUM,
    )
    synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    syllabus: Mapped["Syllabus"] = relationship("Syllabus", back_populates="events")
    user: Mapped["User"] = relationship("User", back_populates="events")
