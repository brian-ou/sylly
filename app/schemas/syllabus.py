"""Syllabus-related Pydantic schemas."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.models.event import ConfidenceLevel, EventType
from app.schemas.event import EventRead


class ParsedEvent(BaseModel):
    """Schema returned by Claude for a single event."""

    title: str
    description: Optional[str] = None
    start_datetime: str
    end_datetime: Optional[str] = None
    is_all_day: bool = False
    recurrence_rule: Optional[str] = None
    event_type: EventType = EventType.OTHER
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM


class ParsedSyllabus(BaseModel):
    """Schema for the full Claude parser output."""

    course_name: Optional[str] = None
    course_code: Optional[str] = None
    term: Optional[str] = None
    timezone: Optional[str] = None
    events: List[ParsedEvent] = Field(default_factory=list)


class SyllabusListItem(BaseModel):
    id: uuid.UUID
    filename: str
    course_name: Optional[str] = None
    term: Optional[str] = None
    event_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class SyllabusDetail(BaseModel):
    id: uuid.UUID
    filename: str
    course_name: Optional[str] = None
    course_code: Optional[str] = None
    term: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    events: List[EventRead]

    model_config = {"from_attributes": True}


class ParseResponse(BaseModel):
    syllabus_id: uuid.UUID
    course_name: Optional[str] = None
    course_code: Optional[str] = None
    term: Optional[str] = None
    events: List[EventRead]
