"""Google OAuth helpers: code exchange, token refresh, profile fetch."""
from __future__ import annotations

import logging
from typing import Any, Dict

import httpx

from app.config import get_settings
from app.exceptions import GoogleAPIError, GoogleAuthExpiredError

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

REQUIRED_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/calendar",
]


async def exchange_code(code: str, redirect_uri: str) -> Dict[str, Any]:
    """Exchange a Google OAuth authorization code for tokens.

    Returns a dict with at least: access_token, refresh_token, expires_in.
    """
    settings = get_settings()
    data = {
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(GOOGLE_TOKEN_URL, data=data)
    except httpx.HTTPError as e:
        logger.exception("Network error during Google token exchange")
        raise GoogleAPIError("Could not reach Google token endpoint") from e

    if resp.status_code != 200:
        logger.warning("Google token exchange failed: %s %s", resp.status_code, resp.text)
        raise GoogleAPIError(
            "Google token exchange failed",
            details={"status": resp.status_code},
        )

    payload = resp.json()
    if "refresh_token" not in payload:
        # Google only returns refresh_token on first consent or with prompt=consent
        logger.warning("Google token response missing refresh_token")
    return payload


async def refresh_access_token(refresh_token: str) -> str:
    """Use a refresh token to get a fresh access token from Google."""
    settings = get_settings()
    data = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(GOOGLE_TOKEN_URL, data=data)
    except httpx.HTTPError as e:
        logger.exception("Network error during Google token refresh")
        raise GoogleAPIError("Could not reach Google token endpoint") from e

    if resp.status_code == 400 or resp.status_code == 401:
        logger.warning("Google refresh token rejected: %s", resp.status_code)
        raise GoogleAuthExpiredError()
    if resp.status_code != 200:
        raise GoogleAPIError(
            "Google token refresh failed",
            details={"status": resp.status_code},
        )

    payload = resp.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise GoogleAPIError("Google did not return an access token")
    return access_token


async def get_user_profile(access_token: str) -> Dict[str, Any]:
    """Fetch the user's Google profile (id, email, name, picture)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(GOOGLE_USERINFO_URL, headers=headers)
    except httpx.HTTPError as e:
        logger.exception("Network error fetching Google userinfo")
        raise GoogleAPIError("Could not reach Google userinfo endpoint") from e

    if resp.status_code == 401:
        raise GoogleAuthExpiredError()
    if resp.status_code != 200:
        raise GoogleAPIError(
            "Google userinfo request failed",
            details={"status": resp.status_code},
        )
    return resp.json()
