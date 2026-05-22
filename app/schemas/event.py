"""Event-related Pydantic schemas."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from app.models.event import ConfidenceLevel, EventType


class EventBase(BaseModel):
    title: str
    description: Optional[str] = None
    start_datetime: datetime
    end_datetime: Optional[datetime] = None
    is_all_day: bool = False
    recurrence_rule: Optional[str] = None
    event_type: EventType = EventType.OTHER
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM


class EventRead(EventBase):
    # Plain UUID string for stored events; composite "<uuid>:<iso>" for
    # expanded recurring instances. The router accepts either form on
    # PATCH/DELETE.
    id: str
    syllabus_id: Optional[uuid.UUID] = None
    google_calendar_id: Optional[str] = None
    google_event_id: Optional[str] = None
    synced_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    # Set when this row is one expanded occurrence of a recurring master
    # event. `master_event_id` is the master's UUID and `occurrence_start`
    # is the original (un-overridden) occurrence start. Both are None for
    # one-off events.
    master_event_id: Optional[uuid.UUID] = None
    occurrence_start: Optional[datetime] = None

    model_config = {"from_attributes": True}

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, v: object) -> object:
        # ORM rows give us a UUID; expansion code gives us a composite str.
        if isinstance(v, uuid.UUID):
            return str(v)
        return v


class EventCreate(BaseModel):
    """Payload for creating a standalone or syllabus-attached event."""

    title: str
    start_datetime: datetime
    description: Optional[str] = None
    end_datetime: Optional[datetime] = None
    is_all_day: bool = False
    recurrence_rule: Optional[str] = None
    event_type: EventType = EventType.OTHER
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH
    syllabus_id: Optional[uuid.UUID] = None


class QuickAddRequest(BaseModel):
    """Natural-language event creation, e.g. 'Study for econ midterm Tue 7pm'."""

    text: str = Field(..., min_length=3, max_length=500)
    syllabus_id: Optional[uuid.UUID] = None


class EventUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    start_datetime: Optional[datetime] = None
    end_datetime: Optional[datetime] = None
    is_all_day: Optional[bool] = None
    recurrence_rule: Optional[str] = None
    event_type: Optional[EventType] = None
    confidence: Optional[ConfidenceLevel] = None


class SyncRequest(BaseModel):
    event_ids: Optional[List[uuid.UUID]] = Field(
        default=None,
        description="Specific event IDs to sync. Defaults to all unsynced events.",
    )
    calendar_name: str = Field(
        ..., description="Name of the Google Calendar to create/use"
    )


class SyncFailure(BaseModel):
    event_id: uuid.UUID
    reason: str


class SyncResponse(BaseModel):
    synced: int
    failed: List[SyncFailure] = []
