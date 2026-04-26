"""Encryption helpers for refresh tokens at rest."""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


@lru_cache
def _get_fernet() -> Fernet:
    settings = get_settings()
    key = settings.REFRESH_TOKEN_ENCRYPTION_KEY
    if isinstance(key, str):
        key = key.encode("utf-8")
    return Fernet(key)


def encrypt_token(plaintext: str) -> str:
    """Encrypt a refresh token; returns a base64 string."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a refresh token; raises ValueError on failure."""
    f = _get_fernet()
    try:
        return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("Invalid encrypted token") from e
