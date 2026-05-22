"""Tests for natural-language quick-add (service + endpoint)."""
from __future__ import annotations

import json
import uuid
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.exceptions import ClaudeParseError, InvalidInputError
from app.services.jwt_tokens import create_access_token
from app.services.quick_add import parse_quick_event
from app.services.rate_limit import chat_limiter


def _mock_client_returning(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    response.usage = MagicMock(input_tokens=10, output_tokens=20)
    client = MagicMock()
    client.messages.create.return_value = response
    return client


def test_parse_quick_event_extracts_fields():
    payload = json.dumps(
        {
            "title": "Study for econ midterm",
            "start_datetime": "2026-05-26T19:00:00",
            "end_datetime": None,
            "is_all_day": False,
            "event_type": "assignment",
            "description": None,
        }
    )
    parsed = parse_quick_event(
        "Study for econ midterm Tuesday 7pm",
        today=date(2026, 5, 21),
        client=_mock_client_returning(payload),
    )
    assert parsed.title == "Study for econ midterm"
    assert parsed.start_datetime == "2026-05-26T19:00:00"
    assert parsed.event_type == "assignment"
    assert parsed.is_all_day is False


def test_parse_quick_event_coerces_unknown_type_to_other():
    payload = json.dumps(
        {
            "title": "Thing",
            "start_datetime": "2026-05-26T19:00:00",
            "event_type": "BOGUS",
        }
    )
    parsed = parse_quick_event("thing", client=_mock_client_returning(payload))
    assert parsed.event_type == "other"


def test_parse_quick_event_vague_input_raises_invalid():
    payload = json.dumps({"title": "", "start_datetime": ""})
    with pytest.raises(InvalidInputError):
        parse_quick_event("???", client=_mock_client_returning(payload))


def test_parse_quick_event_non_json_raises():
    with pytest.raises(ClaudeParseError):
        parse_quick_event("x", client=_mock_client_returning("not json"))


@pytest.mark.asyncio
async def test_quick_add_endpoint_creates_event(app_with_test_db, test_user):
    chat_limiter.reset(str(test_user.id))
    token = create_access_token(test_user.id)
    headers = {"Authorization": f"Bearer {token}"}

    from app.services.quick_add import ParsedQuickEvent

    parsed = ParsedQuickEvent(
        title="Study for econ midterm",
        start_datetime="2026-05-26T19:00:00",
        end_datetime=None,
        is_all_day=False,
        event_type="assignment",
        description=None,
    )
    # Patch the parser (no model call) and the Google sync (no creds in tests).
    with patch(
        "app.routers.events.parse_quick_event", return_value=parsed
    ), patch(
        "app.routers.events.sync_event_to_google", return_value=None
    ):
        transport = ASGITransport(app=app_with_test_db)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/events/quick-add",
                json={"text": "Study for econ midterm Tuesday 7pm"},
                headers=headers,
            )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "Study for econ midterm"
    assert body["event_type"] == "assignment"
    # End defaults to a 1-hour block when the parser returns none.
    assert body["end_datetime"] is not None


@pytest.mark.asyncio
async def test_quick_add_requires_auth(app_with_test_db):
    transport = ASGITransport(app=app_with_test_db)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/events/quick-add", json={"text": "something tomorrow"}
        )
    assert resp.status_code == 401
