"""Tests for JWT creation and validation."""
from __future__ import annotations

import uuid

import pytest

from app.exceptions import UnauthorizedError
from app.services.jwt_tokens import create_access_token, decode_access_token


def test_create_and_decode_token():
    user_id = uuid.uuid4()
    token = create_access_token(user_id)
    assert isinstance(token, str)
    payload = decode_access_token(token)
    assert payload["sub"] == str(user_id)
    assert "exp" in payload
    assert "iat" in payload


def test_decode_invalid_token_raises():
    with pytest.raises(UnauthorizedError):
        decode_access_token("not.a.valid.token")


def test_decode_tampered_token_raises():
    user_id = uuid.uuid4()
    token = create_access_token(user_id)
    tampered = token[:-2] + ("AB" if not token.endswith("AB") else "CD")
    with pytest.raises(UnauthorizedError):
        decode_access_token(tampered)


def test_decode_expired_token_raises():
    """A token with exp in the past should be rejected."""
    from datetime import datetime, timedelta, timezone

    from jose import jwt

    from app.config import get_settings

    settings = get_settings()
    expired_payload = {
        "sub": str(uuid.uuid4()),
        "iat": int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()),
        "exp": int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()),
    }
    expired = jwt.encode(
        expired_payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM
    )
    with pytest.raises(UnauthorizedError):
        decode_access_token(expired)
