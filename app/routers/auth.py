"""Authentication routes: Google OAuth callback, /me, logout."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.exceptions import GoogleAPIError, InvalidInputError
from app.models.user import User
from app.schemas.auth import GoogleCallbackRequest, TokenResponse, UserPublic
from app.services.crypto import encrypt_token
from app.services.google_oauth import exchange_code, get_user_profile
from app.services.jwt_tokens import create_access_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/google/callback",
    response_model=TokenResponse,
    summary="Exchange a Google auth code for a session JWT",
)
async def google_callback(
    body: GoogleCallbackRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Exchange a Google OAuth authorization code for a session JWT.

    Upserts the user record (keyed on Google ID) and stores the encrypted
    refresh token. Returns a bearer JWT plus the user's public profile.
    """
    tokens = await exchange_code(body.code, body.redirect_uri)
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not access_token:
        raise GoogleAPIError("Google did not return an access token")

    profile = await get_user_profile(access_token)
    google_id = profile.get("sub") or profile.get("id")
    email = profile.get("email")
    if not google_id or not email:
        raise InvalidInputError("Google profile missing required fields")

    result = await db.execute(select(User).where(User.google_id == google_id))
    user = result.scalar_one_or_none()

    if user is None:
        if not refresh_token:
            raise InvalidInputError(
                "Google did not return a refresh token; "
                "ensure the OAuth request uses access_type=offline and prompt=consent"
            )
        user = User(
            google_id=google_id,
            email=email,
            name=profile.get("name"),
            picture_url=profile.get("picture"),
            encrypted_refresh_token=encrypt_token(refresh_token),
        )
        db.add(user)
    else:
        user.email = email
        user.name = profile.get("name") or user.name
        user.picture_url = profile.get("picture") or user.picture_url
        if refresh_token:
            user.encrypted_refresh_token = encrypt_token(refresh_token)

    await db.commit()
    await db.refresh(user)

    jwt_token = create_access_token(user.id)
    return TokenResponse(
        access_token=jwt_token,
        token_type="bearer",
        user=UserPublic.model_validate(user),
    )


@router.get(
    "/me",
    response_model=UserPublic,
    summary="Get the currently authenticated user's profile",
)
async def get_me(current_user: User = Depends(get_current_user)) -> UserPublic:
    """Return the profile of the user identified by the bearer token."""
    return UserPublic.model_validate(current_user)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out (client-side; JWTs are stateless)",
)
async def logout(current_user: User = Depends(get_current_user)) -> Response:
    """No-op endpoint. JWTs are stateless; the client should discard the token."""
    return Response(status_code=status.HTTP_204_NO_CONTENT)
