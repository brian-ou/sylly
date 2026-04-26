"""Custom exception classes used throughout the application."""
from __future__ import annotations

from typing import Any, Dict, Optional


class AppError(Exception):
    """Base class for application-specific errors."""

    code: str = "APP_ERROR"
    status_code: int = 500
    message: str = "An application error occurred"

    def __init__(
        self,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message or self.message)
        if message:
            self.message = message
        self.details = details or {}


class ClaudeParseError(AppError):
    code = "CLAUDE_PARSE_ERROR"
    status_code = 502
    message = "Failed to parse syllabus with Claude"


class GoogleAuthExpiredError(AppError):
    code = "GOOGLE_AUTH_EXPIRED"
    status_code = 401
    message = "Google authorization has expired; please re-authenticate"


class GoogleAPIError(AppError):
    code = "GOOGLE_API_UNAVAILABLE"
    status_code = 503
    message = "Google API is currently unavailable"


class PDFTooLargeError(AppError):
    code = "PDF_TOO_LARGE"
    status_code = 413
    message = "PDF exceeds size or page limit"


class RateLimitExceededError(AppError):
    code = "RATE_LIMIT_EXCEEDED"
    status_code = 429
    message = "Rate limit exceeded"


class InvalidInputError(AppError):
    code = "INVALID_INPUT"
    status_code = 400
    message = "Invalid input"


class NotFoundError(AppError):
    code = "NOT_FOUND"
    status_code = 404
    message = "Resource not found"


class UnauthorizedError(AppError):
    code = "UNAUTHORIZED"
    status_code = 401
    message = "Authentication required"


class ForbiddenError(AppError):
    code = "FORBIDDEN"
    status_code = 403
    message = "Forbidden"
