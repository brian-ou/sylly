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
from app.routers.study import (
    study_chat_limiter,
    study_ingest_limiter,
    study_quiz_limiter,
)


def _auth_headers(user_id: uuid.UUID) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def _reset_limits(user_id: uuid.UUID) -> None:
    key = str(user_id)
    parse_limiter.reset(key)
    chat_limiter.reset(key)
    study_ingest_limiter.reset(key)
    study_quiz_limiter.reset(key)
    study_chat_limiter.reset(key)


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


# ---------- PDF ingest ----------


@pytest.mark.asyncio
async def test_ingest_pdf_extracts_concepts(app_with_test_db, test_user):
    _reset_limits(test_user.id)
    headers = _auth_headers(test_user.id)
    extracted = [
        ExtractedConcept(title="Diffusion", summary="Net movement down a gradient."),
    ]
    # Patch BOTH the text extractor (so we don't have to construct a PDF with
    # real glyphs) and the concept extractor (to avoid a real model call).
    with patch(
        "app.routers.study.extract_pdf_text", return_value="x" * 600
    ), patch("app.routers.study.extract_concepts", return_value=extracted):
        # The PDF bytes still need to parse with pypdf so the size+page-count
        # guards pass. Reuse the helper from test_syllabi_parse.
        import io
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        buf = io.BytesIO()
        writer.write(buf)
        pdf_bytes = buf.getvalue()

        transport = ASGITransport(app=app_with_test_db)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/study/material/pdf",
                files={"file": ("notes.pdf", pdf_bytes, "application/pdf")},
                headers=headers,
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [c["title"] for c in body["concepts"]] == ["Diffusion"]


@pytest.mark.asyncio
async def test_ingest_pdf_rejects_image_only_pdf(app_with_test_db, test_user):
    """When pypdf yields too little text, return a clear 400 instead of
    silently shipping a useless prompt to the model."""
    _reset_limits(test_user.id)
    headers = _auth_headers(test_user.id)
    with patch("app.routers.study.extract_pdf_text", return_value=""):
        import io
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        buf = io.BytesIO()
        writer.write(buf)
        pdf_bytes = buf.getvalue()

        transport = ASGITransport(app=app_with_test_db)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/study/material/pdf",
                files={"file": ("scan.pdf", pdf_bytes, "application/pdf")},
                headers=headers,
            )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_ingest_pdf_rejects_non_pdf(app_with_test_db, test_user):
    _reset_limits(test_user.id)
    headers = _auth_headers(test_user.id)
    transport = ASGITransport(app=app_with_test_db)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/study/material/pdf",
            files={"file": ("notes.txt", b"hello", "text/plain")},
            headers=headers,
        )
    assert resp.status_code == 400


# ---------- Study chat (tutor) ----------


@pytest.mark.asyncio
async def test_study_chat_persists_assessments_and_updates_mastery(
    app_with_test_db, test_user, db_session
):
    _reset_limits(test_user.id)
    concept = StudyConcept(
        id=uuid.uuid4(),
        user_id=test_user.id,
        title="Big-O notation",
        summary="Asymptotic upper bound on growth.",
        mastery_score=Decimal("0.300"),
    )
    db_session.add(concept)
    await db_session.commit()

    # study_chat() is patched to return a tutor reply + one high-score
    # assessment. The router should persist a StudyAttempt and bump mastery.
    from app.schemas.study import ConceptAssessment

    assessment = ConceptAssessment(
        concept_id=concept.id, score=0.9, note="Clear definition + example."
    )
    with patch(
        "app.routers.study.study_chat",
        return_value=("Nice — what's a tighter bound here?", [assessment]),
    ):
        headers = _auth_headers(test_user.id)
        transport = ASGITransport(app=app_with_test_db)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/study/chat",
                json={
                    "messages": [
                        {
                            "role": "user",
                            "content": "Big-O is the worst-case asymptotic upper bound.",
                        }
                    ],
                    "concept_id": str(concept.id),
                },
                headers=headers,
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "tighter bound" in body["message"]["content"]
    assert len(body["updated_concepts"]) == 1
    assert body["updated_concepts"][0]["mastery_score"] > 0.3
    assert body["assessments"][0]["score"] == 0.9


@pytest.mark.asyncio
async def test_study_chat_requires_user_last_message(app_with_test_db, test_user):
    _reset_limits(test_user.id)
    headers = _auth_headers(test_user.id)
    transport = ASGITransport(app=app_with_test_db)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/study/chat",
            json={
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ]
            },
            headers=headers,
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_study_chat_returns_404_when_no_concepts(app_with_test_db, test_user):
    _reset_limits(test_user.id)
    headers = _auth_headers(test_user.id)
    transport = ASGITransport(app=app_with_test_db)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/study/chat",
            json={"messages": [{"role": "user", "content": "let's start"}]},
            headers=headers,
        )
    assert resp.status_code == 404
