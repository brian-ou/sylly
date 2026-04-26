"""Authentication-related Pydantic schemas."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


class GoogleCallbackRequest(BaseModel):
    code: str = Field(..., description="Authorization code from Google OAuth")
    redirect_uri: str = Field(..., description="Redirect URI used in the auth flow")


class UserPublic(BaseModel):
    id: uuid.UUID
    email: EmailStr
    name: Optional[str] = None
    picture_url: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserPublic
