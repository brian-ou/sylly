"""Syllabus-related Pydantic schemas."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from app.models.event import ConfidenceLevel, EventType
from app.schemas.event import EventRead
from app.schemas.grade_category import GradeCategoryRead


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


class ParsedGradeCategory(BaseModel):
    """A weighted grade category as parsed from Claude's JSON output.

    Lenient on input: clamps weight to 0..100, defaults missing drop_lowest,
    and trims/validates the name. The Claude parser additionally drops
    entries with empty names and dedupes case-insensitively.
    """

    name: str
    weight: float = 0.0
    drop_lowest: int = 0
    notes: Optional[str] = None

    @field_validator("name", mode="before")
    @classmethod
    def _coerce_name(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v).strip()

    @field_validator("weight", mode="before")
    @classmethod
    def _coerce_weight(cls, v: object) -> float:
        if v is None or v == "":
            return 0.0
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        if f < 0:
            return 0.0
        if f > 100:
            return 100.0
        return f

    @field_validator("drop_lowest", mode="before")
    @classmethod
    def _coerce_drop_lowest(cls, v: object) -> int:
        if v is None or v == "":
            return 0
        try:
            i = int(v)
        except (TypeError, ValueError):
            return 0
        return max(i, 0)


class ParsedSyllabus(BaseModel):
    """Schema for the full Claude parser output."""

    course_name: Optional[str] = None
    course_code: Optional[str] = None
    term: Optional[str] = None
    timezone: Optional[str] = None
    events: List[ParsedEvent] = Field(default_factory=list)
    grade_categories: List[ParsedGradeCategory] = Field(default_factory=list)


class SyllabusListItem(BaseModel):
    id: uuid.UUID
    filename: str
    course_name: Optional[str] = None
    course_code: Optional[str] = None
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
    grade_categories: List[GradeCategoryRead] = Field(default_factory=list)
    weight_sum: float = 0.0

    model_config = {"from_attributes": True}


class ParseResponse(BaseModel):
    syllabus_id: uuid.UUID
    course_name: Optional[str] = None
    course_code: Optional[str] = None
    term: Optional[str] = None
    events: List[EventRead]
    grade_categories: List[GradeCategoryRead] = Field(default_factory=list)
    weight_sum: float = 0.0
