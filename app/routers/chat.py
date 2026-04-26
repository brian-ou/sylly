"""Chat routes: the AI study and scheduling assistant."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.exceptions import InvalidInputError, RateLimitExceededError
from app.models.user import User
from app.schemas.chat import ChatRole, ChatSendRequest, ChatSendResponse
from app.services.chat_agent import chat_plan
from app.services.rate_limit import chat_limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


@router.post(
    "/chat/plan",
    response_model=ChatSendResponse,
    summary="Send a chat turn to the AI study/scheduling assistant",
)
async def chat_plan_endpoint(
    body: ChatSendRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatSendResponse:
    """Send a chat turn to the AI study/scheduling assistant.

    The frontend sends the full conversation history each turn (stateless on
    the backend). The assistant may respond with a plain reply or with
    proposed calendar events the user can accept.
    """
    if not chat_limiter.check(str(current_user.id)):
        raise RateLimitExceededError("Limit is 60 chat calls per hour")

    if not body.messages:
        raise InvalidInputError("messages must not be empty")
    if body.messages[-1].role != ChatRole.USER:
        raise InvalidInputError("last message must be from the user")

    assistant_message, proposed = await chat_plan(
        user=current_user,
        messages=body.messages,
        context=body.context,
        db=db,
    )
    return ChatSendResponse(
        message=assistant_message,
        proposed_events=proposed if proposed else None,
    )
