"""Tests for app/services/chat_agent.py — study context wiring + envelope."""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.models.study_concept import StudyConcept
from app.models.user import User
from app.schemas.chat import ChatMessage, ChatRole
from app.services.chat_agent import _build_system_prompt, chat_plan
from app.services.crypto import encrypt_token


def _user() -> User:
    return User(
        id=uuid.uuid4(),
        google_id="g",
        email="t@example.com",
        encrypted_refresh_token=encrypt_token("r"),
    )


def _concept(title: str, mastery: float, syllabus_id=None) -> StudyConcept:
    return StudyConcept(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        syllabus_id=syllabus_id,
        title=title,
        summary=f"Summary of {title}.",
        mastery_score=Decimal(f"{mastery:.3f}"),
        times_seen=2,
        times_correct=1,
    )


def test_system_prompt_lists_weakest_concepts():
    user = _user()
    concepts = [
        _concept("Strong concept", 0.95),
        _concept("Weak concept A", 0.10),
        _concept("Weak concept B", 0.20),
    ]
    prompt = _build_system_prompt(user, [], [], concepts)
    assert "Weak concept A" in prompt
    assert "Weak concept B" in prompt
    # Mastered count should reflect the >= 0.80 threshold.
    assert "mastered (>=0.80): 1" in prompt


def test_system_prompt_handles_empty_concepts():
    prompt = _build_system_prompt(_user(), [], [], [])
    assert "no study concepts yet" in prompt


@pytest.mark.asyncio
async def test_chat_plan_loads_study_concepts(db_session, test_user):
    db_session.add(
        StudyConcept(
            id=uuid.uuid4(),
            user_id=test_user.id,
            title="Concept Z",
            summary="The summary.",
            mastery_score=Decimal("0.100"),
        )
    )
    await db_session.commit()

    fake_block = MagicMock()
    fake_block.type = "text"
    fake_block.text = (
        "<response>"
        + json.dumps({"message": "Sure — let's quiz Concept Z.", "proposed_events": []})
        + "</response>"
    )
    fake_response = MagicMock()
    fake_response.content = [fake_block]
    fake_response.usage = MagicMock(input_tokens=1, output_tokens=2)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response

    captured: dict = {}

    def _capture_create(**kwargs):
        captured.update(kwargs)
        return fake_response

    fake_client.messages.create.side_effect = _capture_create

    with patch(
        "app.services.chat_agent._build_client", return_value=fake_client
    ):
        message, proposals = await chat_plan(
            user=test_user,
            messages=[
                ChatMessage(
                    id="m1",
                    role=ChatRole.USER,
                    content="quiz me",
                    created_at="2026-05-01T00:00:00Z",
                )
            ],
            context=None,
            db=db_session,
        )

    assert "Concept Z" in captured["system"]
    assert proposals == []
    assert "Concept Z" in message.content
