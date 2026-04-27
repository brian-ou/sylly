"""Per-event Google Calendar sync.

Inserts or updates a single event on Google Calendar and marks the row as
synced. Used by POST /events (auto-sync on create), PATCH /events/{id}
(lazy sync if not yet pushed), and the chat agent's Accept flow.

Calendar selection: events tied to a syllabus reuse whichever calendar that
syllabus's existing events are on; if none, a per-course calendar is created
(named after the course code/name). Standalone events go on a default
"Timely" calendar. This keeps a syllabus's events from fragmenting across
multiple Google calendars over time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.models.syllabus import Syllabus
from app.models.user import User
from app.services.google_calendar import (
    get_calendar_service,
    get_or_create_calendar,
    insert_event as gcal_insert_event,
    update_event as gcal_update_event,
)

logger = logging.getLogger(__name__)

DEFAULT_CALENDAR_NAME = "Timely"


async def _calendar_name_for_syllabus(
    syllabus_id, db: AsyncSession
) -> str:
    """Return a human-friendly calendar name for a syllabus."""
    stmt = select(Syllabus).where(Syllabus.id == syllabus_id)
    syllabus = (await db.execute(stmt)).scalar_one_or_none()
    if syllabus is None:
        return DEFAULT_CALENDAR_NAME
    parts: list[str] = []
    if syllabus.course_code:
        parts.append(syllabus.course_code)
    if syllabus.course_name:
        parts.append(syllabus.course_name)
    return " - ".join(parts) if parts else DEFAULT_CALENDAR_NAME


async def _resolve_calendar_id(
    event: Event, service: Any, db: AsyncSession
) -> str:
    """Pick (and create if needed) the Google calendar id for this event."""
    if event.syllabus_id is not None:
        # Reuse the calendar this syllabus's other events are already on, so
        # all events for one course stay together.
        stmt = (
            select(Event.google_calendar_id)
            .where(
                Event.syllabus_id == event.syllabus_id,
                Event.google_calendar_id.is_not(None),
                Event.id != event.id,
            )
            .limit(1)
        )
        existing = (await db.execute(stmt)).scalar_one_or_none()
        if existing:
            return existing
        name = await _calendar_name_for_syllabus(event.syllabus_id, db)
    else:
        name = DEFAULT_CALENDAR_NAME
    return await get_or_create_calendar(service, name)


async def sync_event_to_google(
    event: Event,
    user: User,
    db: AsyncSession,
    *,
    calendar_name: Optional[str] = None,
) -> None:
    """Insert or update `event` on Google Calendar and mark it synced.

    On success, sets `event.google_calendar_id`, `event.google_event_id`,
    and `event.synced_at`. The caller commits the session.

    Raises GoogleAPIError / GoogleAuthExpiredError on hard failures. The
    caller decides whether to swallow (best-effort) or surface to the user.
    """
    service = await get_calendar_service(user)
    if calendar_name is not None:
        calendar_id = await get_or_create_calendar(service, calendar_name)
    else:
        calendar_id = await _resolve_calendar_id(event, service, db)

    if event.google_event_id and event.google_calendar_id == calendar_id:
        await gcal_update_event(
            service, calendar_id, event.google_event_id, event
        )
    else:
        google_event_id = await gcal_insert_event(service, calendar_id, event)
        event.google_event_id = google_event_id

    event.google_calendar_id = calendar_id
    event.synced_at = datetime.now(timezone.utc)
