"""Chat-related Pydantic schemas for the AI study/scheduling assistant."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel


class ChatRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ChatMessage(BaseModel):
    """A single chat turn. `pending` is a UI-only flag and ignored server-side."""

    id: str
    role: ChatRole
    content: str
    created_at: str
    pending: Optional[bool] = None


class VisibleRange(BaseModel):
    start: datetime
    end: datetime


class ChatContext(BaseModel):
    """Optional UI context — currently just the calendar's visible window."""

    visible_range: Optional[VisibleRange] = None


class ChatSendRequest(BaseModel):
    """Request body for /chat/plan.

    The frontend keeps the conversation history and re-sends the entire log
    each turn. The backend is stateless w.r.t. chat sessions.
    """

    messages: List[ChatMessage]
    context: Optional[ChatContext] = None


class ChatProposedEvent(BaseModel):
    """A calendar event the assistant suggests creating.

    The user accepts or rejects these in the UI; nothing is persisted by the
    chat endpoint itself.
    """

    proposal_id: str
    title: str
    start_datetime: datetime
    end_datetime: Optional[datetime] = None
    event_type: str
    description: Optional[str] = None


class ChatSendResponse(BaseModel):
    message: ChatMessage
    proposed_events: Optional[List[ChatProposedEvent]] = None
