"""Tests for the in-memory sliding-window rate limiter."""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from pypdf import PdfWriter

from app.schemas.syllabus import ParsedSyllabus
from app.services.jwt_tokens import create_access_token
from app.services.rate_limit import SlidingWindowRateLimiter, parse_limiter

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "claude_response.json"


def test_sliding_window_basic():
    rl = SlidingWindowRateLimiter(limit=3, window_seconds=60)
    assert rl.check("user-1") is True
    assert rl.check("user-1") is True
    assert rl.check("user-1") is True
    assert rl.check("user-1") is False
    # Different user not affected
    assert rl.check("user-2") is True


def _make_minimal_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_parse_rate_limit_kicks_in(app_with_test_db, test_user):
    parse_limiter.reset(str(test_user.id))
    pdf_bytes = _make_minimal_pdf()
    fixture_text = FIXTURE_PATH.read_text()
    parsed = ParsedSyllabus.model_validate_json(fixture_text)

    token = create_access_token(test_user.id)
    headers = {"Authorization": f"Bearer {token}"}

    with patch("app.routers.syllabi.parse_syllabus_pdf", return_value=parsed):
        transport = ASGITransport(app=app_with_test_db)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # First 10 should succeed
            for i in range(10):
                files = {"file": (f"t{i}.pdf", pdf_bytes, "application/pdf")}
                resp = await client.post(
                    "/syllabi/parse", files=files, headers=headers
                )
                assert resp.status_code == 200, f"call {i}: {resp.text}"

            # 11th should be rate-limited
            files = {"file": ("over.pdf", pdf_bytes, "application/pdf")}
            resp = await client.post(
                "/syllabi/parse", files=files, headers=headers
            )
            assert resp.status_code == 429
            assert resp.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"

    # Cleanup
    parse_limiter.reset(str(test_user.id))
