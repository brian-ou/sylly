"""End-to-end test for /syllabi/parse with mocked Claude."""
from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from pypdf import PdfWriter

from app.schemas.syllabus import ParsedSyllabus
from app.services.jwt_tokens import create_access_token
from app.services.rate_limit import parse_limiter

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "claude_response.json"


def _make_minimal_pdf() -> bytes:
    """Build a tiny but valid 1-page PDF in-memory."""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_parse_endpoint_creates_syllabus_and_events(
    app_with_test_db, test_user
):
    parse_limiter.reset(str(test_user.id))
    pdf_bytes = _make_minimal_pdf()
    fixture_text = FIXTURE_PATH.read_text()
    parsed = ParsedSyllabus.model_validate_json(fixture_text)

    token = create_access_token(test_user.id)
    headers = {"Authorization": f"Bearer {token}"}

    with patch(
        "app.routers.syllabi.parse_syllabus_pdf", return_value=parsed
    ) as mock_parser:
        transport = ASGITransport(app=app_with_test_db)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            files = {"file": ("test.pdf", pdf_bytes, "application/pdf")}
            data = {"course_hint": "ECON 140", "term_hint": "Fall 2026"}
            resp = await client.post(
                "/syllabi/parse", files=files, data=data, headers=headers
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["course_name"] == "Introduction to Macroeconomics"
    assert body["course_code"] == "ECON 140"
    assert len(body["events"]) == 4
    assert mock_parser.called

    # Grade categories should be persisted alongside events and returned.
    assert len(body["grade_categories"]) == 4
    cat_names = [c["name"] for c in body["grade_categories"]]
    assert cat_names == ["Homework", "Midterm", "Final", "Participation"]
    for cat in body["grade_categories"]:
        assert cat["syllabus_id"] == body["syllabus_id"]
    assert body["weight_sum"] == 100.0

    # GET /syllabi/{id} surfaces them via the Syllabus.grade_categories
    # relationship as well.
    transport = ASGITransport(app=app_with_test_db)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        detail = await client.get(
            f"/syllabi/{body['syllabus_id']}", headers=headers
        )
    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    assert len(detail_body["grade_categories"]) == 4
    assert detail_body["weight_sum"] == 100.0


@pytest.mark.asyncio
async def test_parse_rejects_non_pdf(app_with_test_db, test_user):
    parse_limiter.reset(str(test_user.id))
    token = create_access_token(test_user.id)
    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app_with_test_db)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        files = {"file": ("test.txt", b"hello", "text/plain")}
        resp = await client.post("/syllabi/parse", files=files, headers=headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_parse_requires_auth(app_with_test_db):
    transport = ASGITransport(app=app_with_test_db)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        files = {"file": ("test.pdf", b"%PDF-1.4", "application/pdf")}
        resp = await client.post("/syllabi/parse", files=files)
    assert resp.status_code == 401
