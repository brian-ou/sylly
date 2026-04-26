"""Event routes: list, create, update, delete events."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import List
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user
from app.exceptions import GoogleAPIError, InvalidInputError, NotFoundError
from app.models.event import Event
from app.models.syllabus import Syllabus
from app.models.user import User
from app.schemas.event import EventCreate, EventRead, EventUpdate
from app.services.google_calendar import (
    delete_event as gcal_delete_event,
    get_calendar_service,
    update_event as gcal_update_event,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])


def _localize_naive(dt: datetime) -> datetime:
    """Localize naive datetimes to the configured DEFAULT_TIMEZONE.

    Mirrors `_parse_iso_datetime` in `app/routers/syllabi.py`: PostgreSQL
    `timestamptz` columns + asyncpg reject naive datetimes, so anything coming
    in without a tzinfo is assumed to be in DEFAULT_TIMEZONE (e.g.
    America/Los_Angeles).
    """
    if dt.tzinfo is not None:
        return dt
    try:
        tz = ZoneInfo(get_settings().DEFAULT_TIMEZONE)
    except Exception:
        tz = ZoneInfo("America/Los_Angeles")
    return dt.replace(tzinfo=tz)


@router.get(
    "",
    response_model=List[EventRead],
    summary="List the current user's events within a time range",
)
async def list_events(
    start: datetime = Query(..., description="Inclusive ISO 8601 lower bound"),
    end: datetime = Query(..., description="Exclusive ISO 8601 upper bound"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[EventRead]:
    """Return events whose start_datetime falls in [start, end).

    Both `start` and `end` are required ISO 8601 datetimes. Naive values are
    interpreted in the server's DEFAULT_TIMEZONE. Returns an empty list when
    nothing matches. Ordered by start_datetime ascending.
    """
    if end <= start:
        raise InvalidInputError("end must be strictly greater than start")

    start_dt = _localize_naive(start)
    end_dt = _localize_naive(end)

    stmt = (
        select(Event)
        .where(
            Event.user_id == current_user.id,
            Event.start_datetime >= start_dt,
            Event.start_datetime < end_dt,
        )
        .order_by(Event.start_datetime.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [EventRead.model_validate(e) for e in rows]


@router.post(
    "",
    response_model=EventRead,
    status_code=201,
    summary="Create a standalone or syllabus-attached event",
)
async def create_event(
    body: EventCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventRead:
    """Create a new event for the current user.

    If `syllabus_id` is provided, the syllabus must belong to the current user
    (404 otherwise). Naive datetimes are localized to DEFAULT_TIMEZONE. The
    event is created in an unsynced state — it is not pushed to Google
    Calendar by this endpoint.
    """
    if body.syllabus_id is not None:
        stmt = select(Syllabus).where(
            Syllabus.id == body.syllabus_id,
            Syllabus.user_id == current_user.id,
        )
        syllabus = (await db.execute(stmt)).scalar_one_or_none()
        if syllabus is None:
            raise NotFoundError("Syllabus not found")

    start_dt = _localize_naive(body.start_datetime)
    end_dt = _localize_naive(body.end_datetime) if body.end_datetime else None

    event = Event(
        syllabus_id=body.syllabus_id,
        user_id=current_user.id,
        title=body.title,
        description=body.description,
        start_datetime=start_dt,
        end_datetime=end_dt,
        is_all_day=body.is_all_day,
        recurrence_rule=body.recurrence_rule,
        event_type=body.event_type,
        confidence=body.confidence,
        synced_at=None,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return EventRead.model_validate(event)


@router.patch(
    "/{event_id}",
    response_model=EventRead,
    summary="Update a single event (and Google if synced)",
)
async def update_event_endpoint(
    event_id: uuid.UUID,
    body: EventUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventRead:
    """Update fields on a single event.

    If the event is already synced to Google Calendar, the change is also
    propagated there (best effort).
    """
    stmt = select(Event).where(
        Event.id == event_id, Event.user_id == current_user.id
    )
    event = (await db.execute(stmt)).scalar_one_or_none()
    if event is None:
        raise NotFoundError("Event not found")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(event, field, value)

    if event.google_calendar_id and event.google_event_id:
        try:
            service = await get_calendar_service(current_user)
            await gcal_update_event(
                service, event.google_calendar_id, event.google_event_id, event
            )
        except GoogleAPIError as e:
            logger.warning("Failed to update Google event %s: %s", event.id, e)

    await db.commit()
    await db.refresh(event)
    return EventRead.model_validate(event)


@router.delete(
    "/{event_id}",
    status_code=204,
    summary="Delete an event (and remove it from Google Calendar if synced)",
)
async def delete_event_endpoint(
    event_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an event. Removes it from Google Calendar first if synced."""
    stmt = select(Event).where(
        Event.id == event_id, Event.user_id == current_user.id
    )
    event = (await db.execute(stmt)).scalar_one_or_none()
    if event is None:
        raise NotFoundError("Event not found")

    if event.google_calendar_id and event.google_event_id:
        try:
            service = await get_calendar_service(current_user)
            await gcal_delete_event(
                service, event.google_calendar_id, event.google_event_id
            )
        except Exception as e:
            logger.warning("Failed to delete Google event %s: %s", event.id, e)

    await db.delete(event)
    await db.commit()
