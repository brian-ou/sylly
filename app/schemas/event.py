"""Event-related Pydantic schemas."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

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
    id: uuid.UUID
    syllabus_id: uuid.UUID
    google_calendar_id: Optional[str] = None
    google_event_id: Optional[str] = None
    synced_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


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
