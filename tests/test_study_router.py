"""Integration tests for /study/* endpoints with mocked Claude calls."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.models.event import ConfidenceLevel, Event, EventType
from app.models.study_concept import StudyConcept
from app.models.syllabus import Syllabus
from app.schemas.study import ExtractedConcept
from app.services.jwt_tokens import create_access_token
from app.services.rate_limit import (
    chat_limiter,
    parse_limiter,
)
from app.routers.study import study_ingest_limiter, study_quiz_limiter


def _auth_headers(user_id: uuid.UUID) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def _reset_limits(user_id: uuid.UUID) -> None:
    key = str(user_id)
    parse_limiter.reset(key)
    chat_limiter.reset(key)
    study_ingest_limiter.reset(key)
    study_quiz_limiter.reset(key)


@pytest.mark.asyncio
async def test_ingest_material_creates_concepts(app_with_test_db, test_user):
    _reset_limits(test_user.id)
    headers = _auth_headers(test_user.id)
    extracted = [
        ExtractedConcept(title="Newton's First Law", summary="Inertia."),
        ExtractedConcept(title="Newton's Second Law", summary="F = ma."),
    ]
    with patch("app.routers.study.extract_concepts", return_value=extracted):
        transport = ASGITransport(app=app_with_test_db)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/study/material",
                json={"material": "Newton wrote three laws of motion. " * 5},
                headers=headers,
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["concepts"]) == 2
    titles = {c["title"] for c in body["concepts"]}
    assert titles == {"Newton's First Law", "Newton's Second Law"}


@pytest.mark.asyncio
async def test_ingest_material_dedupes_against_existing(
    app_with_test_db, test_user, db_session
):
    _reset_limits(test_user.id)
    db_session.add(
        StudyConcept(
            id=uuid.uuid4(),
            user_id=test_user.id,
            syllabus_id=None,
            title="Newton's First Law",
            summary="Already saved.",
        )
    )
    await db_session.commit()

    extracted = [
        ExtractedConcept(
            title="newton's first law", summary="Dedupe should drop me."
        ),
        ExtractedConcept(
            title="Newton's Third Law", summary="Action and reaction."
        ),
    ]
    headers = _auth_headers(test_user.id)
    with patch("app.routers.study.extract_concepts", return_value=extracted):
        transport = ASGITransport(app=app_with_test_db)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/study/material",
                json={"material": "Some Newtonian physics intro. " * 5},
                headers=headers,
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [c["title"] for c in body["concepts"]] == ["Newton's Third Law"]


@pytest.mark.asyncio
async def test_quiz_next_returns_404_when_no_concepts(app_with_test_db, test_user):
    _reset_limits(test_user.id)
    headers = _auth_headers(test_user.id)
    transport = ASGITransport(app=app_with_test_db)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/study/quiz/next", json={}, headers=headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_quiz_grade_records_attempt_and_updates_mastery(
    app_with_test_db, test_user, db_session
):
    _reset_limits(test_user.id)
    concept = StudyConcept(
        id=uuid.uuid4(),
        user_id=test_user.id,
        title="Photosynthesis",
        summary="Plants convert light + CO2 + water into glucose + O2.",
        mastery_score=Decimal("0.200"),
        times_seen=1,
        times_correct=0,
    )
    db_session.add(concept)
    await db_session.commit()

    headers = _auth_headers(test_user.id)
    with patch(
        "app.routers.study.grade_answer",
        return_value=(0.9, "Excellent — covered light + reactants."),
    ):
        transport = ASGITransport(app=app_with_test_db)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/study/quiz/grade",
                json={
                    "concept_id": str(concept.id),
                    "question": "What inputs does photosynthesis require?",
                    "answer": "Light, CO2, water; outputs glucose and oxygen.",
                },
                headers=headers,
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["correct"] is True
    assert body["score"] == 0.9
    # Mastery should rise above the prior 0.2 thanks to the EMA update.
    assert body["concept"]["mastery_score"] > 0.2
    assert body["concept"]["times_seen"] == 2
    assert body["concept"]["times_correct"] == 1


@pytest.mark.asyncio
async def test_progress_groups_by_syllabus(app_with_test_db, test_user, db_session):
    _reset_limits(test_user.id)
    syllabus = Syllabus(
        id=uuid.uuid4(),
        user_id=test_user.id,
        filename="cs161.pdf",
        course_name="Algorithms",
        course_code="CS 161",
        parsed_events={},
    )
    db_session.add(syllabus)
    await db_session.commit()

    db_session.add_all(
        [
            StudyConcept(
                id=uuid.uuid4(),
                user_id=test_user.id,
                syllabus_id=syllabus.id,
                title="DP",
                summary="Dynamic programming.",
                mastery_score=Decimal("0.900"),
                times_seen=3,
                times_correct=3,
            ),
            StudyConcept(
                id=uuid.uuid4(),
                user_id=test_user.id,
                syllabus_id=syllabus.id,
                title="Greedy",
                summary="Greedy algorithms.",
                mastery_score=Decimal("0.200"),
                times_seen=4,
                times_correct=0,
            ),
            StudyConcept(
                id=uuid.uuid4(),
                user_id=test_user.id,
                syllabus_id=None,
                title="Misc",
                summary="Unscoped concept.",
            ),
        ]
    )
    db_session.add(
        Event(
            id=uuid.uuid4(),
            user_id=test_user.id,
            syllabus_id=syllabus.id,
            title="CS 161 Midterm",
            start_datetime=datetime.now(timezone.utc) + timedelta(days=10),
            event_type=EventType.EXAM,
            confidence=ConfidenceLevel.HIGH,
        )
    )
    await db_session.commit()

    headers = _auth_headers(test_user.id)
    transport = ASGITransport(app=app_with_test_db)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/study/progress", headers=headers)
    assert resp.status_code == 200, resp.text
    buckets = resp.json()["buckets"]
    by_label = {b["course_label"]: b for b in buckets}
    assert "CS 161" in by_label
    cs = by_label["CS 161"]
    assert cs["concept_count"] == 2
    assert cs["mastered_count"] == 1
    assert cs["struggling_count"] == 1
    assert cs["next_exam_at"] is not None
