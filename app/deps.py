"""FastAPI dependencies (current user, db session)."""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.exceptions import UnauthorizedError
from app.models.user import User
from app.services.jwt_tokens import decode_access_token


async def get_current_user(
    authorization: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve the current user from the Authorization: Bearer <jwt> header."""
    if not authorization:
        raise UnauthorizedError("Missing Authorization header")

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise UnauthorizedError("Authorization header must be 'Bearer <token>'")
    token = parts[1]

    payload = decode_access_token(token)
    sub = payload.get("sub")
    if not sub:
        raise UnauthorizedError("Token missing subject")

    try:
        user_id = uuid.UUID(sub)
    except (ValueError, TypeError) as e:
        raise UnauthorizedError("Invalid subject in token") from e

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise UnauthorizedError("User not found")
    return user
