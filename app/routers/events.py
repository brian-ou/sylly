"""Event routes: list, create, update, delete events.

Recurring events:
  - Stored as a single "master" row with a `recurrence_rule` (RRULE).
  - `GET /events` expands the master into individual occurrences using
    `dateutil.rrule`. Each occurrence is returned with a composite id
    `"<master_uuid>:<occurrence_iso>"` plus `master_event_id` and
    `occurrence_start` so the frontend can act on a specific occurrence.
  - `PATCH` / `DELETE` accept either a plain UUID (whole row / whole series
    via `apply_to_series=true`) or a composite id (per-occurrence).
  - Per-occurrence edits create an "override" row pointing back to the
    master via `override_of_id` + `override_original_start`. The original
    occurrence is added to the master's `exdates` so the regular expansion
    skips it and the override is rendered in its place. Per-occurrence
    deletes append to `exdates` (and remove any existing override).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user
from app.exceptions import GoogleAPIError, InvalidInputError, NotFoundError
from app.models.event import Event
from app.models.syllabus import Syllabus
from app.models.user import User
from app.schemas.event import EventCreate, EventRead, EventUpdate
from app.services.event_sync import sync_event_to_google
from app.services.google_calendar import (
    delete_event as gcal_delete_event,
    delete_recurring_instance,
    get_calendar_service,
    update_event as gcal_update_event,
    update_recurring_instance,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------


def _localize_naive(dt: datetime) -> datetime:
    """Localize naive datetimes to the configured DEFAULT_TIMEZONE."""
    if dt.tzinfo is not None:
        return dt
    try:
        tz = ZoneInfo(get_settings().DEFAULT_TIMEZONE)
    except Exception:
        tz = ZoneInfo("America/Los_Angeles")
    return dt.replace(tzinfo=tz)


def _to_utc(dt: datetime) -> datetime:
    """Normalize a datetime to UTC for instant-equality comparisons."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Composite-id parsing
# ---------------------------------------------------------------------------


def _parse_event_id(event_id: str) -> Tuple[uuid.UUID, Optional[datetime]]:
    """Parse a plain UUID or a composite '<uuid>:<iso>' id.

    Returns (master_uuid, occurrence_start | None). Raises InvalidInputError
    on malformed input.
    """
    raw = event_id.strip()
    if ":" in raw and len(raw.split(":", 1)[0]) == 36:
        master_str, occ_iso = raw.split(":", 1)
        try:
            return uuid.UUID(master_str), datetime.fromisoformat(occ_iso)
        except (TypeError, ValueError) as e:
            raise InvalidInputError(f"Malformed composite event id: {raw}") from e
    try:
        return uuid.UUID(raw), None
    except (TypeError, ValueError) as e:
        raise InvalidInputError(f"Malformed event id: {raw}") from e


# ---------------------------------------------------------------------------
# Recurrence expansion
# ---------------------------------------------------------------------------


def _parse_exdates(raw: List[str]) -> set:
    """Parse a list of ISO datetime strings into a set of UTC instants."""
    out: set = set()
    for s in raw or []:
        try:
            dt = datetime.fromisoformat(s)
            out.add(_to_utc(dt))
        except (TypeError, ValueError):
            continue
    return out


def _instance_read(
    master: Event,
    occ_start: datetime,
    duration: timedelta,
    override: Optional[Event],
) -> EventRead:
    """Build an EventRead for one expanded occurrence.

    If `override` is set, its fields take precedence (this occurrence has
    been edited individually); otherwise the master's fields are used with
    start/end shifted to `occ_start`.
    """
    composite_id = f"{master.id}:{occ_start.isoformat()}"
    if override is not None:
        data = EventRead.model_validate(override).model_dump()
        data["id"] = composite_id
        data["master_event_id"] = master.id
        data["occurrence_start"] = occ_start
        # Overrides themselves don't carry a recurrence_rule; surface the
        # master's so the UI can show the recurring badge.
        data["recurrence_rule"] = master.recurrence_rule
        return EventRead(**data)

    data = EventRead.model_validate(master).model_dump()
    data["id"] = composite_id
    data["master_event_id"] = master.id
    data["occurrence_start"] = occ_start
    data["start_datetime"] = occ_start
    data["end_datetime"] = occ_start + duration
    return EventRead(**data)


def _expand_master(
    master: Event,
    range_start: datetime,
    range_end: datetime,
    overrides_by_orig_start_utc: dict,
) -> List[EventRead]:
    """Expand a recurring master into instance EventReads in [start, end)."""
    rule_str = master.recurrence_rule or ""
    if not rule_str.upper().startswith("RRULE:"):
        rule_str = f"RRULE:{rule_str}"
    try:
        rr = rrulestr(rule_str, dtstart=master.start_datetime)
    except (ValueError, TypeError) as e:
        logger.warning(
            "Could not parse recurrence_rule for event %s: %s", master.id, e
        )
        return [EventRead.model_validate(master)]

    duration = (
        master.end_datetime - master.start_datetime
        if master.end_datetime
        else timedelta(hours=1)
    )
    exdate_set = _parse_exdates(master.exdates or [])

    out: List[EventRead] = []
    for occ_start in rr.between(range_start, range_end, inc=True):
        occ_utc = _to_utc(occ_start)
        override = overrides_by_orig_start_utc.get(occ_utc)
        if override is not None:
            out.append(_instance_read(master, occ_start, duration, override))
        elif occ_utc in exdate_set:
            # Deleted occurrence with no override — skip entirely.
            continue
        else:
            out.append(_instance_read(master, occ_start, duration, None))
    return out


# ---------------------------------------------------------------------------
# GET /events
# ---------------------------------------------------------------------------


async def query_events_in_range(
    user_id: uuid.UUID,
    start_dt: datetime,
    end_dt: datetime,
    db: AsyncSession,
) -> List[EventRead]:
    """Return events overlapping [start_dt, end_dt) with recurrences expanded.

    Shared by the GET /events route and the chat agent's `list_events` tool.
    Both `start_dt` and `end_dt` must be timezone-aware.
    """
    # Top-level rows: non-recurring events whose start is in range, plus any
    # recurring master that *might* have an occurrence in range (its master
    # start may be earlier than range_start).
    stmt = (
        select(Event)
        .where(
            Event.user_id == user_id,
            Event.override_of_id.is_(None),
            or_(
                and_(
                    Event.recurrence_rule.is_(None),
                    Event.start_datetime >= start_dt,
                    Event.start_datetime < end_dt,
                ),
                and_(
                    Event.recurrence_rule.is_not(None),
                    Event.start_datetime < end_dt,
                ),
            ),
        )
        .order_by(Event.start_datetime.asc())
    )
    masters = list((await db.execute(stmt)).scalars().all())

    # Pull all overrides for the recurring masters in one query, indexed by
    # (master_id, override_original_start_utc).
    recurring_ids = [m.id for m in masters if m.recurrence_rule is not None]
    overrides_by_master: dict[uuid.UUID, dict] = {}
    if recurring_ids:
        ostmt = select(Event).where(
            Event.user_id == user_id,
            Event.override_of_id.in_(recurring_ids),
        )
        for o in (await db.execute(ostmt)).scalars().all():
            if o.override_original_start is None:
                continue
            key = _to_utc(o.override_original_start)
            overrides_by_master.setdefault(o.override_of_id, {})[key] = o

    out: List[EventRead] = []
    for m in masters:
        if m.recurrence_rule is None:
            out.append(EventRead.model_validate(m))
        else:
            out.extend(
                _expand_master(
                    m, start_dt, end_dt, overrides_by_master.get(m.id, {})
                )
            )

    out.sort(key=lambda e: e.start_datetime)
    return out


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
    """Return events overlapping [start, end), with recurrences expanded."""
    if end <= start:
        raise InvalidInputError("end must be strictly greater than start")

    start_dt = _localize_naive(start)
    end_dt = _localize_naive(end)
    return await query_events_in_range(current_user.id, start_dt, end_dt, db)


# ---------------------------------------------------------------------------
# POST /events
# ---------------------------------------------------------------------------


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
    """Create a new event and best-effort push it to Google Calendar."""
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
    # Events without an end time default to a 1-hour block (full day for
    # all-day). The website and Google Calendar render zero-length events
    # poorly.
    if end_dt is None:
        end_dt = start_dt + (
            timedelta(days=1) if body.is_all_day else timedelta(hours=1)
        )

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

    # Best-effort push to Google Calendar. On failure the row stays in our
    # DB unsynced; the user can retry later or via the syllabus sync flow.
    try:
        await sync_event_to_google(event, current_user, db)
        await db.commit()
        await db.refresh(event)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to sync new event %s to Google Calendar: %s",
            event.id,
            exc,
        )

    return EventRead.model_validate(event)


# ---------------------------------------------------------------------------
# PATCH /events/{event_id}
# ---------------------------------------------------------------------------


def _apply_update(target: Event, update_data: dict) -> None:
    """Apply a validated EventUpdate dict onto an Event row in place."""
    for field, value in update_data.items():
        if field in ("start_datetime", "end_datetime") and value is not None:
            value = _localize_naive(value)
        setattr(target, field, value)


async def _find_override(
    master_id: uuid.UUID, occurrence_start: datetime, db: AsyncSession
) -> Optional[Event]:
    """Find an existing override row for a specific occurrence, if any."""
    occ_utc = _to_utc(occurrence_start)
    stmt = select(Event).where(Event.override_of_id == master_id)
    for o in (await db.execute(stmt)).scalars().all():
        if o.override_original_start is not None and _to_utc(
            o.override_original_start
        ) == occ_utc:
            return o
    return None


@router.patch(
    "/{event_id}",
    response_model=EventRead,
    summary="Update an event, occurrence, or entire series",
)
async def update_event_endpoint(
    event_id: str,
    body: EventUpdate,
    apply_to_series: bool = Query(
        False,
        description=(
            "If the id is a composite occurrence id, set true to apply the "
            "patch to the entire recurring series (the master row) instead "
            "of just this occurrence."
        ),
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventRead:
    """Update fields on a single event, an occurrence, or the whole series.

    The `event_id` may be a plain UUID (the row itself) or the composite
    `"<master_uuid>:<occurrence_iso>"` form returned by `GET /events` for
    expanded recurrences. With `apply_to_series=true`, the patch is applied
    to the master regardless.
    """
    master_id, occurrence_start = _parse_event_id(event_id)

    stmt = select(Event).where(
        Event.id == master_id, Event.user_id == current_user.id
    )
    master = (await db.execute(stmt)).scalar_one_or_none()
    if master is None:
        raise NotFoundError("Event not found")

    update_data = body.model_dump(exclude_unset=True)

    # Case 1: edit the master / whole series.
    if occurrence_start is None or apply_to_series:
        _apply_update(master, update_data)

        # Push to Google. If never synced, do a lazy first sync now.
        if master.google_calendar_id and master.google_event_id:
            try:
                service = await get_calendar_service(current_user)
                await gcal_update_event(
                    service,
                    master.google_calendar_id,
                    master.google_event_id,
                    master,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to update Google event %s: %s", master.id, exc
                )
        else:
            try:
                await sync_event_to_google(master, current_user, db)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to sync event %s to Google: %s", master.id, exc
                )

        await db.commit()
        await db.refresh(master)
        return EventRead.model_validate(master)

    # Case 2: per-occurrence edit on a recurring master.
    if master.recurrence_rule is None:
        raise InvalidInputError(
            "Composite occurrence ids are only valid for recurring events"
        )

    override = await _find_override(master.id, occurrence_start, db)

    if override is None:
        # Build a new override row inheriting from the master, applying the patch.
        duration = (
            master.end_datetime - master.start_datetime
            if master.end_datetime
            else timedelta(hours=1)
        )
        new_start = update_data.get("start_datetime", occurrence_start)
        if new_start is not None:
            new_start = _localize_naive(new_start)
        new_end = update_data.get("end_datetime")
        if new_end is not None:
            new_end = _localize_naive(new_end)
        elif "start_datetime" in update_data:
            # Caller moved the start without giving an end: keep duration.
            new_end = new_start + duration
        else:
            new_end = occurrence_start + duration

        override = Event(
            user_id=master.user_id,
            syllabus_id=master.syllabus_id,
            title=update_data.get("title", master.title),
            description=update_data.get("description", master.description),
            start_datetime=new_start,
            end_datetime=new_end,
            is_all_day=update_data.get("is_all_day", master.is_all_day),
            recurrence_rule=None,
            event_type=update_data.get("event_type", master.event_type),
            confidence=update_data.get("confidence", master.confidence),
            override_of_id=master.id,
            override_original_start=occurrence_start,
            synced_at=None,
        )
        db.add(override)

        # Mark the original occurrence as exception on the master so the
        # regular expansion skips it (the override is rendered in its place).
        new_exdates = list(master.exdates or [])
        occ_iso = occurrence_start.isoformat()
        if occ_iso not in new_exdates:
            new_exdates.append(occ_iso)
            master.exdates = new_exdates
    else:
        _apply_update(override, update_data)

    # Sync this single occurrence to Google.
    if master.google_calendar_id and master.google_event_id:
        try:
            service = await get_calendar_service(current_user)
            await update_recurring_instance(
                service,
                master.google_calendar_id,
                master.google_event_id,
                occurrence_start,
                override,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to update Google instance for %s at %s: %s",
                master.id,
                occurrence_start.isoformat(),
                exc,
            )

    await db.commit()
    await db.refresh(override)

    data = EventRead.model_validate(override).model_dump()
    data["id"] = f"{master.id}:{occurrence_start.isoformat()}"
    data["master_event_id"] = master.id
    data["occurrence_start"] = occurrence_start
    data["recurrence_rule"] = master.recurrence_rule
    return EventRead(**data)


# ---------------------------------------------------------------------------
# DELETE /events/{event_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/{event_id}",
    status_code=204,
    summary="Delete an event, occurrence, or entire series",
)
async def delete_event_endpoint(
    event_id: str,
    apply_to_series: bool = Query(
        False,
        description=(
            "If the id is a composite occurrence id, set true to delete the "
            "entire recurring series instead of just this occurrence."
        ),
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an event, a single occurrence, or the whole recurring series."""
    master_id, occurrence_start = _parse_event_id(event_id)

    stmt = select(Event).where(
        Event.id == master_id, Event.user_id == current_user.id
    )
    master = (await db.execute(stmt)).scalar_one_or_none()
    if master is None:
        raise NotFoundError("Event not found")

    # Case 1: delete the master / whole series (cascades to override rows
    # via FK ON DELETE CASCADE).
    if occurrence_start is None or apply_to_series:
        if master.google_calendar_id and master.google_event_id:
            try:
                service = await get_calendar_service(current_user)
                await gcal_delete_event(
                    service,
                    master.google_calendar_id,
                    master.google_event_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to delete Google event %s: %s", master.id, exc
                )
        await db.delete(master)
        await db.commit()
        return

    # Case 2: delete a single occurrence of a recurring master.
    if master.recurrence_rule is None:
        raise InvalidInputError(
            "Composite occurrence ids are only valid for recurring events"
        )

    override = await _find_override(master.id, occurrence_start, db)
    if override is not None:
        await db.delete(override)

    new_exdates = list(master.exdates or [])
    occ_iso = occurrence_start.isoformat()
    if occ_iso not in new_exdates:
        new_exdates.append(occ_iso)
        master.exdates = new_exdates

    if master.google_calendar_id and master.google_event_id:
        try:
            service = await get_calendar_service(current_user)
            await delete_recurring_instance(
                service,
                master.google_calendar_id,
                master.google_event_id,
                occurrence_start,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to delete Google instance for %s at %s: %s",
                master.id,
                occurrence_start.isoformat(),
                exc,
            )

    await db.commit()
