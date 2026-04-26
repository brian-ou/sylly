"""Tests for the Google Calendar service body construction (no network)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.models.event import ConfidenceLevel, Event, EventType
from app.services.google_calendar import (
    event_to_google_body,
    get_or_create_calendar,
    insert_event,
)


def _make_event(
    *,
    is_all_day: bool = False,
    recurrence_rule: str | None = None,
    end: datetime | None = None,
) -> Event:
    return Event(
        id=uuid.uuid4(),
        syllabus_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="Lecture",
        description="Intro",
        start_datetime=datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc),
        end_datetime=end,
        is_all_day=is_all_day,
        recurrence_rule=recurrence_rule,
        event_type=EventType.LECTURE,
        confidence=ConfidenceLevel.HIGH,
    )


def test_event_to_google_body_timed():
    ev = _make_event(end=datetime(2026, 8, 26, 11, 0, tzinfo=timezone.utc))
    body = event_to_google_body(ev, "America/Los_Angeles")
    assert body["summary"] == "Lecture"
    assert "dateTime" in body["start"]
    assert body["start"]["timeZone"] == "America/Los_Angeles"
    assert "dateTime" in body["end"]
    assert "recurrence" not in body


def test_event_to_google_body_all_day():
    ev = _make_event(is_all_day=True)
    body = event_to_google_body(ev, "America/Los_Angeles")
    assert "date" in body["start"]
    assert "dateTime" not in body["start"]


def test_event_to_google_body_with_rrule():
    ev = _make_event(
        recurrence_rule="FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=20261205T235959Z"
    )
    body = event_to_google_body(ev, "America/Los_Angeles")
    assert body["recurrence"] == [
        "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=20261205T235959Z"
    ]


def test_event_to_google_body_rrule_already_prefixed():
    ev = _make_event(recurrence_rule="RRULE:FREQ=DAILY;COUNT=5")
    body = event_to_google_body(ev, "UTC")
    assert body["recurrence"] == ["RRULE:FREQ=DAILY;COUNT=5"]


@pytest.mark.asyncio
async def test_get_or_create_calendar_returns_existing():
    service = MagicMock()
    listing_request = MagicMock()
    listing_request.execute.return_value = {
        "items": [
            {"id": "existing-id", "summary": "Course: ECON 140"},
            {"id": "other-id", "summary": "Personal"},
        ]
    }
    service.calendarList.return_value.list.return_value = listing_request

    cid = await get_or_create_calendar(service, "Course: ECON 140")
    assert cid == "existing-id"
    service.calendars.assert_not_called()


@pytest.mark.asyncio
async def test_get_or_create_calendar_creates_when_missing():
    service = MagicMock()
    listing_request = MagicMock()
    listing_request.execute.return_value = {"items": []}
    service.calendarList.return_value.list.return_value = listing_request

    insert_request = MagicMock()
    insert_request.execute.return_value = {"id": "new-cal-id", "summary": "X"}
    service.calendars.return_value.insert.return_value = insert_request

    cid = await get_or_create_calendar(service, "X")
    assert cid == "new-cal-id"


@pytest.mark.asyncio
async def test_insert_event_returns_google_id():
    service = MagicMock()
    insert_request = MagicMock()
    insert_request.execute.return_value = {"id": "google-event-123"}
    service.events.return_value.insert.return_value = insert_request

    ev = _make_event(end=datetime(2026, 8, 26, 11, 0, tzinfo=timezone.utc))
    gid = await insert_event(service, "cal-id", ev)
    assert gid == "google-event-123"
