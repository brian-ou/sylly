"""Application configuration loaded from environment variables.

Fails fast on startup if any required variable is missing.
"""
from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Pydantic settings model. All values come from env vars or .env file."""

    # Database
    DATABASE_URL: str

    # Anthropic
    ANTHROPIC_API_KEY: str
    ANTHROPIC_MODEL: str = "claude-haiku-4-5"

    # Google OAuth
    GOOGLE_CLIENT_ID: str
    GOOGLE_CLIENT_SECRET: str
    GOOGLE_REDIRECT_URI: str

    # JWT
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 10080

    # Encryption
    REFRESH_TOKEN_ENCRYPTION_KEY: str

    # CORS
    ALLOWED_ORIGINS: str = "http://localhost:3000"

    # App
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"

    # Limits
    MAX_PDF_BYTES: int = 20 * 1024 * 1024  # 20MB
    MAX_PDF_PAGES: int = 100
    PARSE_RATE_LIMIT: int = 10  # per hour per user

    # Default timezone when syllabus doesn't specify one
    DEFAULT_TIMEZONE: str = "America/Los_Angeles"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    @field_validator("ALLOWED_ORIGINS")
    @classmethod
    def _validate_origins(cls, v: str) -> str:
        if not v:
            raise ValueError("ALLOWED_ORIGINS must not be empty")
        return v

    @property
    def allowed_origins_list(self) -> List[str]:
        """Comma-separated origins parsed into a list."""
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor; raises on missing env vars."""
    return Settings()  # type: ignore[call-arg]
