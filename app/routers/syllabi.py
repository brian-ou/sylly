"""Syllabus routes: parse, list, get, delete, sync."""
from __future__ import annotations

import io
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user
from app.exceptions import (
    GoogleAPIError,
    InvalidInputError,
    NotFoundError,
    PDFTooLargeError,
    RateLimitExceededError,
)
from app.models.event import ConfidenceLevel, Event, EventType
from app.models.grade_category import GradeCategory
from app.models.syllabus import Syllabus
from app.models.user import User
from app.schemas.event import EventRead, SyncFailure, SyncRequest, SyncResponse
from app.schemas.grade_category import GradeCategoryRead
from app.schemas.syllabus import ParseResponse, SyllabusDetail, SyllabusListItem
from app.services.claude_parser import parse_syllabus_pdf
from app.services.google_calendar import (
    delete_event as gcal_delete_event,
    get_calendar_service,
    get_or_create_calendar,
    insert_event as gcal_insert_event,
    update_event as gcal_update_event,
)
from app.services.rate_limit import parse_limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["syllabi"])

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._\- ]")


def _sanitize_filename(name: str) -> str:
    """Strip path separators and unsafe characters from an uploaded filename."""
    base = name.replace("\\", "/").split("/")[-1]
    cleaned = _FILENAME_SAFE.sub("_", base).strip()
    return cleaned[:255] or "syllabus.pdf"


def _parse_iso_datetime(s: str) -> datetime:
    """Parse an ISO 8601 datetime string into a timezone-aware datetime.

    PostgreSQL `timestamptz` columns + asyncpg reject naive datetimes, so any
    naive value coming back from Claude must be localized. Per the spec, naive
    timestamps are assumed to be in the configured DEFAULT_TIMEZONE (e.g.
    America/Los_Angeles).
    """
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise InvalidInputError(f"Invalid datetime: {s}") from e

    if dt.tzinfo is None:
        try:
            tz = ZoneInfo(get_settings().DEFAULT_TIMEZONE)
        except Exception:
            tz = ZoneInfo("America/Los_Angeles")
        dt = dt.replace(tzinfo=tz)
    return dt


def _cap_recurrence_at_term_end(rrule: str, term_end: datetime) -> str:
    """Ensure an RRULE's UNTIL clause is no later than `term_end`.

    Claude sometimes omits UNTIL on recurring events (e.g. weekly office hours),
    which causes Google Calendar to extend them forever. This caps every
    recurrence at the term's last dated event. If UNTIL is missing, it is
    added; if present but later than `term_end`, it is replaced; if already
    earlier, it is left alone.
    """
    if not rrule:
        return rrule

    # Normalize: strip "RRULE:" prefix if present
    upper = rrule.upper()
    if upper.startswith("RRULE:"):
        body = rrule[len("RRULE:"):]
        prefix = "RRULE:"
    else:
        body = rrule
        prefix = ""

    until_utc = term_end.astimezone(timezone.utc)
    until_str = until_utc.strftime("%Y%m%dT%H%M%SZ")

    parts = body.split(";")
    found_until = False
    new_parts: List[str] = []
    for part in parts:
        if part.upper().startswith("UNTIL="):
            found_until = True
            existing = part[len("UNTIL="):]
            try:
                if "T" in existing:
                    existing_dt = datetime.strptime(existing, "%Y%m%dT%H%M%SZ")
                else:
                    existing_dt = datetime.strptime(existing, "%Y%m%d")
                existing_dt = existing_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                existing_dt = None

            if existing_dt is None or existing_dt > until_utc:
                new_parts.append(f"UNTIL={until_str}")
            else:
                new_parts.append(part)
        else:
            new_parts.append(part)

    if not found_until:
        new_parts.append(f"UNTIL={until_str}")

    return prefix + ";".join(new_parts)


@router.post(
    "/syllabi/parse",
    response_model=ParseResponse,
    summary="Upload a syllabus PDF, parse with Claude, and stage events",
)
async def parse_syllabus(
    file: UploadFile = File(..., description="Syllabus PDF (<=20MB, <=100 pages)"),
    course_hint: Optional[str] = Form(default=None),
    term_hint: Optional[str] = Form(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ParseResponse:
    """Upload a syllabus PDF and extract dated events.

    Validates the PDF, sends it to Claude, and saves the parsed syllabus and
    its events. Events are NOT pushed to Google Calendar by this endpoint.
    """
    settings = get_settings()

    # Rate limit (per user, sliding window)
    if not parse_limiter.check(str(current_user.id)):
        raise RateLimitExceededError(
            f"Limit is {settings.PARSE_RATE_LIMIT} parse calls per hour"
        )

    if file.content_type != "application/pdf":
        raise InvalidInputError("File must be application/pdf")

    pdf_bytes = await file.read()
    if len(pdf_bytes) > settings.MAX_PDF_BYTES:
        raise PDFTooLargeError(
            f"PDF exceeds maximum size of {settings.MAX_PDF_BYTES} bytes"
        )

    try:
        reader = PdfReader(stream=io.BytesIO(pdf_bytes))
        page_count = len(reader.pages)
    except PdfReadError as e:
        raise InvalidInputError(f"Could not read PDF: {e}") from e
    except Exception as e:  # pypdf can raise non-PdfReadError on malformed input
        raise InvalidInputError(f"Could not read PDF: {e}") from e

    if page_count > settings.MAX_PDF_PAGES:
        raise PDFTooLargeError(
            f"PDF has {page_count} pages; max is {settings.MAX_PDF_PAGES}"
        )

    parsed = parse_syllabus_pdf(
        pdf_bytes,
        course_hint=course_hint,
        term_hint=term_hint,
    )

    safe_name = _sanitize_filename(file.filename or "syllabus.pdf")

    syllabus = Syllabus(
        user_id=current_user.id,
        filename=safe_name,
        course_name=parsed.course_name,
        course_code=parsed.course_code,
        term=parsed.term,
        parsed_events=parsed.model_dump(mode="json"),
    )
    db.add(syllabus)
    await db.flush()

    # First pass: parse all dates so we can compute the term-end cap.
    # The latest event date in the syllabus (typically the final exam) is the
    # natural end of the academic term. We use this to cap recurring events
    # like office hours so they don't extend indefinitely on Google Calendar.
    parsed_events: List[tuple] = []  # (pe, start_dt, end_dt)
    candidate_term_ends: List[datetime] = []
    for pe in parsed.events:
        try:
            start_dt = _parse_iso_datetime(pe.start_datetime)
        except Exception:
            logger.warning("Skipping event with invalid start_datetime: %s", pe.title)
            continue
        end_dt: Optional[datetime] = None
        if pe.end_datetime:
            try:
                end_dt = _parse_iso_datetime(pe.end_datetime)
            except Exception:
                end_dt = None

        parsed_events.append((pe, start_dt, end_dt))
        # Term-end candidate is the latest non-recurring event date, since
        # recurring events without UNTIL would otherwise dominate.
        if not pe.recurrence_rule:
            candidate_term_ends.append(end_dt or start_dt)

    term_end: Optional[datetime] = (
        max(candidate_term_ends) if candidate_term_ends else None
    )
    if term_end is not None:
        logger.info(
            "Detected term end for syllabus %s: %s", syllabus.id, term_end.isoformat()
        )

    db_events: List[Event] = []
    for pe, start_dt, end_dt in parsed_events:
        rrule = pe.recurrence_rule
        if rrule and term_end is not None:
            rrule = _cap_recurrence_at_term_end(rrule, term_end)

        ev = Event(
            syllabus_id=syllabus.id,
            user_id=current_user.id,
            title=pe.title,
            description=pe.description,
            start_datetime=start_dt,
            end_datetime=end_dt,
            is_all_day=bool(pe.is_all_day),
            recurrence_rule=rrule,
            event_type=EventType(pe.event_type),
            confidence=ConfidenceLevel(pe.confidence),
            synced_at=None,
        )
        db.add(ev)
        db_events.append(ev)

    db_categories: List[GradeCategory] = []
    for idx, pgc in enumerate(parsed.grade_categories):
        gc = GradeCategory(
            syllabus_id=syllabus.id,
            name=pgc.name,
            weight=pgc.weight,
            drop_lowest=pgc.drop_lowest,
            notes=pgc.notes,
            sort_order=idx,
        )
        db.add(gc)
        db_categories.append(gc)

    await db.commit()
    for ev in db_events:
        await db.refresh(ev)
    for gc in db_categories:
        await db.refresh(gc)
    await db.refresh(syllabus)

    return ParseResponse(
        syllabus_id=syllabus.id,
        course_name=syllabus.course_name,
        course_code=syllabus.course_code,
        term=syllabus.term,
        events=[EventRead.model_validate(e) for e in db_events],
        grade_categories=[GradeCategoryRead.model_validate(gc) for gc in db_categories],
        weight_sum=float(sum(float(gc.weight) for gc in db_categories)),
    )


@router.get(
    "/syllabi",
    response_model=List[SyllabusListItem],
    summary="List the current user's syllabi",
)
async def list_syllabi(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[SyllabusListItem]:
    """Return the current user's syllabi, including event counts."""
    stmt = (
        select(
            Syllabus.id,
            Syllabus.filename,
            Syllabus.course_name,
            Syllabus.term,
            Syllabus.created_at,
            func.count(Event.id).label("event_count"),
        )
        .outerjoin(Event, Event.syllabus_id == Syllabus.id)
        .where(Syllabus.user_id == current_user.id)
        .group_by(Syllabus.id)
        .order_by(Syllabus.created_at.desc())
    )
    rows = (await db.execute(stmt)).all()
    return [
        SyllabusListItem(
            id=r.id,
            filename=r.filename,
            course_name=r.course_name,
            term=r.term,
            event_count=r.event_count,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.get(
    "/syllabi/{syllabus_id}",
    response_model=SyllabusDetail,
    summary="Get a syllabus and its events",
)
async def get_syllabus(
    syllabus_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SyllabusDetail:
    """Return a single syllabus and its associated events."""
    stmt = (
        select(Syllabus)
        .options(
            selectinload(Syllabus.events),
            selectinload(Syllabus.grade_categories),
        )
        .where(Syllabus.id == syllabus_id, Syllabus.user_id == current_user.id)
    )
    syllabus = (await db.execute(stmt)).scalar_one_or_none()
    if syllabus is None:
        raise NotFoundError("Syllabus not found")
    return SyllabusDetail(
        id=syllabus.id,
        filename=syllabus.filename,
        course_name=syllabus.course_name,
        course_code=syllabus.course_code,
        term=syllabus.term,
        created_at=syllabus.created_at,
        updated_at=syllabus.updated_at,
        events=[EventRead.model_validate(e) for e in syllabus.events],
        grade_categories=[
            GradeCategoryRead.model_validate(gc) for gc in syllabus.grade_categories
        ],
        weight_sum=float(sum(float(gc.weight) for gc in syllabus.grade_categories)),
    )


@router.delete(
    "/syllabi/{syllabus_id}",
    status_code=204,
    summary="Delete a syllabus and its events (also from Google Calendar)",
)
async def delete_syllabus(
    syllabus_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a syllabus. Synced events are best-effort removed from Google."""
    stmt = (
        select(Syllabus)
        .options(
            selectinload(Syllabus.events),
            selectinload(Syllabus.grade_categories),
        )
        .where(Syllabus.id == syllabus_id, Syllabus.user_id == current_user.id)
    )
    syllabus = (await db.execute(stmt)).scalar_one_or_none()
    if syllabus is None:
        raise NotFoundError("Syllabus not found")

    synced_events = [
        e for e in syllabus.events if e.google_calendar_id and e.google_event_id
    ]
    if synced_events:
        try:
            service = await get_calendar_service(current_user)
        except Exception as e:
            logger.warning(
                "Could not get calendar service while deleting syllabus %s: %s",
                syllabus_id,
                e,
            )
            service = None
        if service is not None:
            for e in synced_events:
                try:
                    await gcal_delete_event(
                        service, e.google_calendar_id, e.google_event_id
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to delete Google event %s: %s",
                        e.google_event_id,
                        exc,
                    )

    await db.delete(syllabus)
    await db.commit()


@router.post(
    "/syllabi/{syllabus_id}/sync",
    response_model=SyncResponse,
    summary="Push events for a syllabus to Google Calendar",
)
async def sync_syllabus(
    syllabus_id: uuid.UUID,
    body: SyncRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SyncResponse:
    """Push events to a Google Calendar named `calendar_name`.

    If `event_ids` is omitted, all unsynced events for the syllabus are pushed.
    The operation is idempotent: events with an existing google_event_id are
    updated rather than duplicated.
    """
    stmt = (
        select(Syllabus)
        .options(selectinload(Syllabus.events))
        .where(Syllabus.id == syllabus_id, Syllabus.user_id == current_user.id)
    )
    syllabus = (await db.execute(stmt)).scalar_one_or_none()
    if syllabus is None:
        raise NotFoundError("Syllabus not found")

    if body.event_ids:
        wanted = set(body.event_ids)
        events = [e for e in syllabus.events if e.id in wanted]
    else:
        events = [e for e in syllabus.events if e.synced_at is None]

    if not events:
        return SyncResponse(synced=0, failed=[])

    service = await get_calendar_service(current_user)
    calendar_id = await get_or_create_calendar(service, body.calendar_name)

    synced_count = 0
    failures: List[SyncFailure] = []

    for ev in events:
        try:
            if ev.google_event_id and ev.google_calendar_id == calendar_id:
                await gcal_update_event(
                    service, calendar_id, ev.google_event_id, ev
                )
            else:
                google_event_id = await gcal_insert_event(service, calendar_id, ev)
                ev.google_event_id = google_event_id
            ev.google_calendar_id = calendar_id
            ev.synced_at = datetime.now(timezone.utc)
            synced_count += 1
        except GoogleAPIError as e:
            failures.append(SyncFailure(event_id=ev.id, reason=str(e)))
        except Exception as e:
            logger.exception("Unexpected error syncing event %s", ev.id)
            failures.append(SyncFailure(event_id=ev.id, reason=str(e)))

    await db.commit()

    return SyncResponse(synced=synced_count, failed=failures)
