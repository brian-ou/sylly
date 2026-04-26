"""Tests for the Claude parser service (mocked Anthropic client)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.exceptions import ClaudeParseError
from app.schemas.syllabus import ParsedSyllabus
from app.services.claude_parser import parse_syllabus_pdf

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "claude_response.json"


def _mock_anthropic_with_text(text: str) -> MagicMock:
    """Build a mocked Anthropic client whose messages.create returns the given text."""
    fake_block = MagicMock()
    fake_block.type = "text"
    fake_block.text = text

    fake_response = MagicMock()
    fake_response.content = [fake_block]
    fake_response.usage = MagicMock(input_tokens=100, output_tokens=200)

    client = MagicMock()
    client.messages.create.return_value = fake_response
    return client


def test_parse_syllabus_returns_parsed_model():
    fixture_text = FIXTURE_PATH.read_text()
    client = _mock_anthropic_with_text(fixture_text)

    result = parse_syllabus_pdf(
        pdf_bytes=b"%PDF-1.4 fake",
        course_hint="ECON 140",
        term_hint="Fall 2026",
        client=client,
    )
    assert isinstance(result, ParsedSyllabus)
    assert result.course_code == "ECON 140"
    assert result.term == "Fall 2026"
    assert len(result.events) == 4
    titles = [e.title for e in result.events]
    assert "Midterm Exam" in titles

    # Grade categories surface from the same JSON contract.
    assert len(result.grade_categories) == 4
    by_name = {gc.name: gc for gc in result.grade_categories}
    assert by_name["Homework"].weight == 30
    assert by_name["Homework"].drop_lowest == 1
    assert by_name["Final"].notes == "must pass final to pass course"
    assert sum(gc.weight for gc in result.grade_categories) == 100


def test_parse_grade_categories_clamp_and_dedupe():
    payload = {
        "course_name": "X",
        "events": [],
        "grade_categories": [
            {"name": "Homework", "weight": 200, "drop_lowest": -1, "notes": None},
            {"name": "homework", "weight": 10},  # dupe (case-insensitive)
            {"name": "  ", "weight": 5},  # blank name -> dropped
            {"name": "Midterm", "weight": -5},  # negative -> 0
            {"name": "Final", "weight": "not a number"},  # bad -> 0
        ],
    }
    client = _mock_anthropic_with_text(json.dumps(payload))
    result = parse_syllabus_pdf(pdf_bytes=b"%PDF-1.4 fake", client=client)

    names = [gc.name for gc in result.grade_categories]
    assert names == ["Homework", "Midterm", "Final"]
    by_name = {gc.name: gc for gc in result.grade_categories}
    assert by_name["Homework"].weight == 100  # clamped down
    assert by_name["Homework"].drop_lowest == 0  # clamped up from -1
    assert by_name["Midterm"].weight == 0
    assert by_name["Final"].weight == 0


def test_parse_missing_grade_categories_defaults_to_empty():
    payload = {"course_name": "X", "events": []}
    client = _mock_anthropic_with_text(json.dumps(payload))
    result = parse_syllabus_pdf(pdf_bytes=b"%PDF-1.4 fake", client=client)
    assert result.grade_categories == []


def test_parse_strips_code_fences():
    fixture_text = FIXTURE_PATH.read_text()
    fenced = f"```json\n{fixture_text}\n```"
    client = _mock_anthropic_with_text(fenced)
    result = parse_syllabus_pdf(pdf_bytes=b"%PDF-1.4 fake", client=client)
    assert result.course_code == "ECON 140"


def test_parse_invalid_json_raises():
    client = _mock_anthropic_with_text("not actually json")
    with pytest.raises(ClaudeParseError):
        parse_syllabus_pdf(pdf_bytes=b"%PDF-1.4 fake", client=client)


def test_parse_empty_response_raises():
    client = _mock_anthropic_with_text("")
    with pytest.raises(ClaudeParseError):
        parse_syllabus_pdf(pdf_bytes=b"%PDF-1.4 fake", client=client)


def test_parse_schema_mismatch_raises():
    client = _mock_anthropic_with_text(json.dumps({"unexpected": "shape"}))
    # Empty events list is technically valid; ensure we still validate types.
    # Send something with a wrong event_type to force validation failure.
    bad = {
        "course_name": "X",
        "events": [
            {
                "title": "T",
                "start_datetime": "2026-01-01T00:00:00",
                "is_all_day": True,
                "event_type": "BOGUS",
                "confidence": "high",
            }
        ],
    }
    client = _mock_anthropic_with_text(json.dumps(bad))
    with pytest.raises(ClaudeParseError):
        parse_syllabus_pdf(pdf_bytes=b"%PDF-1.4 fake", client=client)
