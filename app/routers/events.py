"""Event routes: PATCH and DELETE individual events."""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.exceptions import GoogleAPIError, NotFoundError
from app.models.event import Event
from app.models.user import User
from app.schemas.event import EventRead, EventUpdate
from app.services.google_calendar import (
    delete_event as gcal_delete_event,
    get_calendar_service,
    update_event as gcal_update_event,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])


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
