"""Chat-related Pydantic schemas for the AI study/scheduling assistant."""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class ChatRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ChatMessage(BaseModel):
    """A single chat turn.

    On the wire `id` and `created_at` are optional so the frontend can post a
    minimal `{role, content}` for inbound messages — the agent only reads
    role/content anyway, and stamps a fresh id + timestamp on the assistant
    reply it returns. `pending` is a UI-only flag and ignored server-side.
    """

    id: Optional[str] = None
    role: ChatRole
    content: str
    created_at: Optional[str] = None
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
    is_all_day: bool = False
    recurrence_rule: Optional[str] = None
    syllabus_id: Optional[uuid.UUID] = None


class ChatProposedUpdate(BaseModel):
    """An edit the assistant proposes for an existing event.

    The frontend sends `patch` verbatim as the body of
    `PATCH /events/{event_id}?apply_to_series=...` when the user accepts.
    """

    proposal_id: str
    # Plain UUID for one-off events; composite "<master_uuid>:<occurrence_iso>"
    # for a single occurrence of a recurring series.
    event_id: str
    apply_to_series: bool = False
    patch: Dict[str, Any]
    # One-line summary the assistant generates for the tray card header,
    # e.g. "Move 'CS 161 study' from 4pm to 5pm".
    summary: Optional[str] = None


class ChatProposedDelete(BaseModel):
    """A deletion the assistant proposes for an existing event."""

    proposal_id: str
    event_id: str
    apply_to_series: bool = False
    summary: Optional[str] = None


class ChatSendResponse(BaseModel):
    message: ChatMessage
    proposed_events: Optional[List[ChatProposedEvent]] = None
    proposed_updates: Optional[List[ChatProposedUpdate]] = None
    proposed_deletes: Optional[List[ChatProposedDelete]] = None
