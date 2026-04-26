"""Tests for the get_current_user FastAPI dependency."""
from __future__ import annotations

import uuid

import pytest

from app.deps import get_current_user
from app.exceptions import UnauthorizedError
from app.services.jwt_tokens import create_access_token


@pytest.mark.asyncio
async def test_missing_authorization_header(db_session):
    with pytest.raises(UnauthorizedError):
        await get_current_user(authorization=None, db=db_session)


@pytest.mark.asyncio
async def test_malformed_authorization_header(db_session):
    with pytest.raises(UnauthorizedError):
        await get_current_user(authorization="NotBearer abc", db=db_session)


@pytest.mark.asyncio
async def test_invalid_token(db_session):
    with pytest.raises(UnauthorizedError):
        await get_current_user(authorization="Bearer not.a.token", db=db_session)


@pytest.mark.asyncio
async def test_token_for_unknown_user(db_session):
    token = create_access_token(uuid.uuid4())
    with pytest.raises(UnauthorizedError):
        await get_current_user(authorization=f"Bearer {token}", db=db_session)


@pytest.mark.asyncio
async def test_valid_token_returns_user(db_session, test_user):
    token = create_access_token(test_user.id)
    user = await get_current_user(authorization=f"Bearer {token}", db=db_session)
    assert user.id == test_user.id
