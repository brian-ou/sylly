"""Idempotency test for /syllabi/{id}/sync (mocked Google client)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.models.event import ConfidenceLevel, Event, EventType
from app.models.syllabus import Syllabus
from app.services.jwt_tokens import create_access_token


@pytest.mark.asyncio
async def test_sync_is_idempotent(app_with_test_db, db_session, test_user):
    # Seed a syllabus with one event
    syl = Syllabus(
        id=uuid.uuid4(),
        user_id=test_user.id,
        filename="x.pdf",
        course_name="ECON 140",
        course_code="ECON 140",
        term="Fall 2026",
        parsed_events={"events": []},
    )
    db_session.add(syl)
    await db_session.commit()
    await db_session.refresh(syl)

    ev = Event(
        id=uuid.uuid4(),
        syllabus_id=syl.id,
        user_id=test_user.id,
        title="Lecture",
        description="Intro",
        start_datetime=datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc),
        end_datetime=datetime(2026, 8, 26, 11, 0, tzinfo=timezone.utc),
        is_all_day=False,
        event_type=EventType.LECTURE,
        confidence=ConfidenceLevel.HIGH,
    )
    db_session.add(ev)
    await db_session.commit()
    await db_session.refresh(ev)

    token = create_access_token(test_user.id)
    headers = {"Authorization": f"Bearer {token}"}

    fake_service = MagicMock()
    insert_calls = {"n": 0}
    update_calls = {"n": 0}

    async def _fake_get_service(user):
        return fake_service

    async def _fake_get_or_create_calendar(service, name):
        return "cal-123"

    async def _fake_insert(service, calendar_id, event, default_timezone=None):
        insert_calls["n"] += 1
        return f"google-event-{insert_calls['n']}"

    async def _fake_update(service, calendar_id, gid, event, default_timezone=None):
        update_calls["n"] += 1

    with patch(
        "app.routers.syllabi.get_calendar_service", side_effect=_fake_get_service
    ), patch(
        "app.routers.syllabi.get_or_create_calendar",
        side_effect=_fake_get_or_create_calendar,
    ), patch(
        "app.routers.syllabi.gcal_insert_event", side_effect=_fake_insert
    ), patch(
        "app.routers.syllabi.gcal_update_event", side_effect=_fake_update
    ):
        transport = ASGITransport(app=app_with_test_db)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            payload = {"calendar_name": "Course: ECON 140"}
            r1 = await client.post(
                f"/syllabi/{syl.id}/sync", json=payload, headers=headers
            )
            assert r1.status_code == 200, r1.text
            assert r1.json()["synced"] == 1
            assert insert_calls["n"] == 1

            # Calling sync again with the same event_id should update, not insert.
            payload2 = {
                "calendar_name": "Course: ECON 140",
                "event_ids": [str(ev.id)],
            }
            r2 = await client.post(
                f"/syllabi/{syl.id}/sync", json=payload2, headers=headers
            )
            assert r2.status_code == 200, r2.text
            assert r2.json()["synced"] == 1

    # Insert should only have been called once; update should be called the second time.
    assert insert_calls["n"] == 1
    assert update_calls["n"] == 1
