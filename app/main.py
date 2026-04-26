"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
import time
import uuid
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.exceptions import AppError
from app.routers import auth as auth_router
from app.routers import events as events_router
from app.routers import grade_categories as grade_categories_router
from app.routers import syllabi as syllabi_router


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


_settings = get_settings()
_configure_logging(_settings.LOG_LEVEL)
logger = logging.getLogger("syllabus_calendar")


app = FastAPI(
    title="Syllabus-to-Calendar API",
    description=(
        "Upload course syllabi (PDF), extract dated events with Claude, "
        "and sync them to Google Calendar."
    ),
    version="0.1.0",
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Log every request: method, path, user_id (if any), status, duration."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[JSONResponse]],
    ) -> JSONResponse:
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = int((time.monotonic() - started) * 1000)
            logger.exception(
                "request_id=%s method=%s path=%s status=500 duration_ms=%s",
                request_id,
                request.method,
                request.url.path,
                duration_ms,
            )
            raise
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "request_id=%s method=%s path=%s status=%s duration_ms=%s",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response


app.add_middleware(RequestLogMiddleware)


def _error_response(
    code: str, message: str, status_code: int, details: dict | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            }
        },
    )


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return _error_response(exc.code, exc.message, exc.status_code, exc.details)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return _error_response(
        code="INVALID_INPUT",
        message="Request validation failed",
        status_code=422,
        details={"errors": jsonable_encoder(exc.errors())},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    logger.exception("Unhandled exception: %s", exc)
    return _error_response(
        code="INTERNAL_ERROR",
        message="An internal server error occurred",
        status_code=500,
    )


app.include_router(auth_router.router)
app.include_router(syllabi_router.router)
app.include_router(events_router.router)
app.include_router(grade_categories_router.router)


@app.get("/health", tags=["meta"], summary="Liveness probe")
async def health() -> dict:
    """Simple health endpoint for load balancers."""
    return {"status": "ok"}
