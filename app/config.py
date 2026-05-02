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
    # Conversational chat agent. Stays on a slightly stronger default since it
    # has to juggle calendar context and tutoring tone.
    ANTHROPIC_MODEL: str = "claude-haiku-4-5"
    # Parsing-class workloads: PDF text extraction, study concept extraction,
    # quiz generation, grading. These are bounded JSON tasks where Haiku 4.5's
    # speed/cost beats anything bigger.
    ANTHROPIC_PARSE_MODEL: str = "claude-haiku-4-5"
    # Below this character count, fall back to sending the raw PDF instead of
    # extracted text — the PDF probably is image-based and pypdf got nothing.
    PARSE_TEXT_MIN_CHARS: int = 400

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

    @field_validator("DATABASE_URL")
    @classmethod
    def _normalize_database_url(cls, v: str) -> str:
        """Auto-upgrade plain postgresql:// URLs (e.g. from Railway/Heroku) to
        the async driver asyncpg, which is what our SQLAlchemy engine expects.
        """
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            v = "postgresql+asyncpg://" + v[len("postgresql://"):]
        return v

    @property
    def allowed_origins_list(self) -> List[str]:
        """Comma-separated origins parsed into a list."""
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor; raises on missing env vars."""
    return Settings()  # type: ignore[call-arg]
