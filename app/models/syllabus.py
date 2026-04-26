"""Syllabus ORM model."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, List

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.event import Event


class Syllabus(Base):
    """An uploaded syllabus and its parsed metadata."""

    __tablename__ = "syllabi"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    course_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    course_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    term: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parsed_events: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict)

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

    user: Mapped["User"] = relationship("User", back_populates="syllabi")
    events: Mapped[List["Event"]] = relationship(
        "Event", back_populates="syllabus", cascade="all, delete-orphan"
    )
