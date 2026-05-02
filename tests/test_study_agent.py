"""Tests for app/services/study_agent.py with mocked Anthropic clients."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.exceptions import ClaudeParseError
from app.models.study_concept import StudyConcept
from app.services.study_agent import (
    CORRECT_THRESHOLD,
    apply_attempt_to_concept,
    extract_concepts,
    generate_question,
    grade_answer,
    update_mastery,
)


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


def _stub_concept() -> StudyConcept:
    return StudyConcept(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="Stack vs heap",
        summary="Stack stores fixed-size frames; heap stores dynamic allocations.",
        mastery_score=Decimal("0.500"),
        times_seen=2,
        times_correct=1,
    )


def test_extract_concepts_dedupes_and_caps():
    payload = json.dumps(
        [
            {"title": "Mitosis", "summary": "Cell division producing two identical cells."},
            {"title": "mitosis", "summary": "Duplicate to test dedupe."},
            {"title": "Meiosis", "summary": "Cell division producing four gametes."},
            {"title": "", "summary": "Skipped — blank title."},
            {"title": "Photosynthesis", "summary": ""},  # skipped — blank summary
        ]
    )
    client = _mock_client_returning(payload)
    concepts = extract_concepts("a long enough material " * 20, client=client)
    assert [c.title for c in concepts] == ["Mitosis", "Meiosis"]


def test_extract_concepts_strips_code_fences():
    inner = json.dumps([{"title": "Big-O", "summary": "Asymptotic complexity."}])
    fenced = f"```json\n{inner}\n```"
    concepts = extract_concepts("x" * 100, client=_mock_client_returning(fenced))
    assert len(concepts) == 1
    assert concepts[0].title == "Big-O"


def test_extract_concepts_non_json_raises():
    client = _mock_client_returning("not json at all")
    with pytest.raises(ClaudeParseError):
        extract_concepts("x" * 100, client=client)


def test_extract_concepts_non_array_raises():
    client = _mock_client_returning(json.dumps({"not": "an array"}))
    with pytest.raises(ClaudeParseError):
        extract_concepts("x" * 100, client=client)


def test_generate_question_returns_text():
    client = _mock_client_returning("Why does the heap allow dynamic allocation?")
    q = generate_question(_stub_concept(), recent_attempts=[], client=client)
    assert "heap" in q.lower()


def test_generate_question_empty_raises():
    client = _mock_client_returning("")
    with pytest.raises(ClaudeParseError):
        generate_question(_stub_concept(), recent_attempts=[], client=client)


def test_grade_answer_parses_score_and_feedback():
    client = _mock_client_returning(
        json.dumps({"score": 0.85, "feedback": "Solid — small nuance missed."})
    )
    score, feedback = grade_answer(
        _stub_concept(), "Why heap?", "It stores dynamic stuff.", client=client
    )
    assert score == 0.85
    assert "Solid" in feedback


def test_grade_answer_clamps_score():
    client = _mock_client_returning(json.dumps({"score": 5.0, "feedback": "ok"}))
    score, _ = grade_answer(_stub_concept(), "q", "a", client=client)
    assert score == 1.0

    client = _mock_client_returning(json.dumps({"score": -2, "feedback": "ok"}))
    score, _ = grade_answer(_stub_concept(), "q", "a", client=client)
    assert score == 0.0


def test_grade_answer_non_json_raises():
    client = _mock_client_returning("nope")
    with pytest.raises(ClaudeParseError):
        grade_answer(_stub_concept(), "q", "a", client=client)


def test_update_mastery_is_ema_like():
    # A perfect answer pulls 0.5 toward 1.0 but doesn't snap to 1.0.
    new = update_mastery(0.5, 1.0)
    assert 0.5 < new < 1.0
    # A zero answer pulls down without going below 0.
    new = update_mastery(0.5, 0.0)
    assert 0.0 <= new < 0.5


def test_apply_attempt_increments_correct_only_above_threshold():
    c = _stub_concept()
    prior_seen = c.times_seen
    prior_correct = c.times_correct
    now = datetime.now(timezone.utc)

    apply_attempt_to_concept(c, score=CORRECT_THRESHOLD, attempted_at=now)
    assert c.times_seen == prior_seen + 1
    assert c.times_correct == prior_correct + 1

    apply_attempt_to_concept(c, score=CORRECT_THRESHOLD - 0.01, attempted_at=now)
    assert c.times_seen == prior_seen + 2
    assert c.times_correct == prior_correct + 1  # not incremented this time
    assert c.last_attempted_at == now
