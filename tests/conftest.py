"""Shared pytest fixtures.

Sets dummy env vars BEFORE importing the app so config.get_settings doesn't fail,
and configures an in-memory SQLite database for tests.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from typing import AsyncGenerator

import pytest
import pytest_asyncio

# Set required env vars before any app imports
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("GOOGLE_REDIRECT_URI", "http://localhost:3000/auth/callback")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-do-not-use-in-prod")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("JWT_EXPIRE_MINUTES", "60")

# Generate a real Fernet key for tests
from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("REFRESH_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")

# Make PostgreSQL UUID and JSONB types compile to SQLite-compatible SQL.
# This must happen BEFORE any model module is imported.
from sqlalchemy.dialects.postgresql import JSONB, UUID  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(element, compiler, **kw):  # type: ignore[no-untyped-def]
    return "CHAR(36)"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # type: ignore[no-untyped-def]
    return "JSON"


from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

# Import after env is configured and compilers are registered
from app.database import Base  # noqa: E402
from app.models import event as _event_module  # noqa: E402,F401
from app.models import grade_category as _grade_category_module  # noqa: E402,F401
from app.models import syllabus as _syllabus_module  # noqa: E402,F401
from app.models import user as _user_module  # noqa: E402,F401


@pytest.fixture(scope="session")
def event_loop():
    """Provide a session-scoped event loop for async fixtures."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="function")
async def test_engine():
    """Create a fresh in-memory SQLite engine for each test.

    Uses StaticPool + a single shared connection so all sessions see the same
    in-memory database (default SQLite memory DBs are per-connection).
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Yield a fresh AsyncSession bound to the test engine."""
    SessionLocal = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with SessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def app_with_test_db(test_engine):
    """Build a FastAPI app instance with get_db overridden to use the test engine."""
    from app.database import get_db
    from app.main import app

    SessionLocal = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def _override_get_db():
        async with SessionLocal() as session:
            try:
                yield session
            finally:
                await session.close()

    app.dependency_overrides[get_db] = _override_get_db
    yield app
    app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture
async def test_user(db_session: AsyncSession):
    """Create and return a persisted test user."""
    from app.models.user import User
    from app.services.crypto import encrypt_token

    user = User(
        id=uuid.uuid4(),
        google_id="google-test-user-id",
        email="test@example.com",
        name="Test User",
        picture_url=None,
        encrypted_refresh_token=encrypt_token("fake-refresh-token"),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user
