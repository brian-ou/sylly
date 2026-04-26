"""Google Calendar API wrapper."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from google.auth.transport.requests import Request as GoogleRequest  # noqa: F401
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import get_settings
from app.exceptions import GoogleAPIError, GoogleAuthExpiredError
from app.models.event import Event
from app.models.user import User
from app.services.crypto import decrypt_token
from app.services.google_oauth import REQUIRED_SCOPES, refresh_access_token

logger = logging.getLogger(__name__)


async def get_calendar_service(user: User) -> Any:
    """Build a Google Calendar API service client for the given user.

    Decrypts the user's refresh token, obtains a fresh access token,
    and returns an authorized googleapiclient resource.
    """
    settings = get_settings()
    try:
        refresh_token = decrypt_token(user.encrypted_refresh_token)
    except ValueError as e:
        logger.error("Failed to decrypt user refresh token for user_id=%s", user.id)
        raise GoogleAuthExpiredError("Stored refresh token is invalid") from e

    access_token = await refresh_access_token(refresh_token)

    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=REQUIRED_SCOPES,
    )
    # The discovery build call is sync; run in default executor to avoid blocking.
    loop = asyncio.get_event_loop()
    service = await loop.run_in_executor(
        None,
        lambda: build("calendar", "v3", credentials=creds, cache_discovery=False),
    )
    return service


def _execute_with_retry(request, max_retries: int = 3) -> Any:
    """Execute a Google API request with exponential backoff on rate limits."""
    delay = 1.0
    for attempt in range(max_retries):
        try:
            return request.execute()
        except HttpError as e:
            status = getattr(e, "status_code", None) or (
                e.resp.status if hasattr(e, "resp") else None
            )
            if status in (401, 403):
                # Distinguish auth-expired (401) from quota/permissions (403)
                if status == 401:
                    raise GoogleAuthExpiredError() from e
                # 403 with rateLimitExceeded reason → retry, otherwise raise
                reason = ""
                try:
                    reason = e.error_details[0].get("reason", "") if e.error_details else ""
                except Exception:
                    pass
                if "rateLimit" in reason or "userRateLimit" in reason:
                    logger.warning("Google API rate limit hit, retry %s", attempt + 1)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise GoogleAPIError(
                    f"Google API error: {e}", details={"status": status}
                ) from e
            if status in (429, 500, 502, 503, 504):
                logger.warning(
                    "Google API transient error %s, retry %s", status, attempt + 1
                )
                time.sleep(delay)
                delay *= 2
                continue
            raise GoogleAPIError(
                f"Google API error: {e}", details={"status": status}
            ) from e
        except Exception as e:
            logger.exception("Unexpected error calling Google API")
            raise GoogleAPIError("Unexpected Google API error") from e
    raise GoogleAPIError("Google API request failed after retries")


async def get_or_create_calendar(service: Any, name: str) -> str:
    """Return calendar_id for the named calendar, creating it if missing."""
    loop = asyncio.get_event_loop()

    def _list() -> Dict[str, Any]:
        return _execute_with_retry(service.calendarList().list())

    listing = await loop.run_in_executor(None, _list)
    for item in listing.get("items", []):
        if item.get("summary") == name:
            return item["id"]

    def _create() -> Dict[str, Any]:
        return _execute_with_retry(
            service.calendars().insert(body={"summary": name})
        )

    created = await loop.run_in_executor(None, _create)
    return created["id"]


def event_to_google_body(event: Event, default_timezone: str) -> Dict[str, Any]:
    """Convert an internal Event to the Google Calendar API event body."""
    body: Dict[str, Any] = {
        "summary": event.title,
    }
    if event.description:
        body["description"] = event.description

    if event.is_all_day:
        # All-day events use 'date' not 'dateTime'
        start_date = event.start_datetime.date().isoformat()
        end_date = (
            event.end_datetime.date().isoformat()
            if event.end_datetime
            else start_date
        )
        body["start"] = {"date": start_date}
        body["end"] = {"date": end_date}
    else:
        body["start"] = {
            "dateTime": event.start_datetime.isoformat(),
            "timeZone": default_timezone,
        }
        end = event.end_datetime or event.start_datetime
        body["end"] = {
            "dateTime": end.isoformat(),
            "timeZone": default_timezone,
        }

    if event.recurrence_rule:
        rule = event.recurrence_rule
        if not rule.upper().startswith("RRULE:"):
            rule = f"RRULE:{rule}"
        body["recurrence"] = [rule]

    return body


async def insert_event(
    service: Any,
    calendar_id: str,
    event: Event,
    default_timezone: Optional[str] = None,
) -> str:
    """Insert an event into the given calendar; returns google_event_id."""
    settings = get_settings()
    tz = default_timezone or settings.DEFAULT_TIMEZONE
    body = event_to_google_body(event, tz)

    loop = asyncio.get_event_loop()

    def _insert() -> Dict[str, Any]:
        return _execute_with_retry(
            service.events().insert(calendarId=calendar_id, body=body)
        )

    created = await loop.run_in_executor(None, _insert)
    return created["id"]


async def update_event(
    service: Any,
    calendar_id: str,
    google_event_id: str,
    event: Event,
    default_timezone: Optional[str] = None,
) -> None:
    """Update an existing Google Calendar event."""
    settings = get_settings()
    tz = default_timezone or settings.DEFAULT_TIMEZONE
    body = event_to_google_body(event, tz)

    loop = asyncio.get_event_loop()

    def _update() -> Dict[str, Any]:
        return _execute_with_retry(
            service.events().update(
                calendarId=calendar_id,
                eventId=google_event_id,
                body=body,
            )
        )

    await loop.run_in_executor(None, _update)


async def delete_event(
    service: Any, calendar_id: str, google_event_id: str
) -> None:
    """Delete an event from Google Calendar (best-effort, may swallow 404)."""
    loop = asyncio.get_event_loop()

    def _delete() -> None:
        try:
            _execute_with_retry(
                service.events().delete(
                    calendarId=calendar_id, eventId=google_event_id
                )
            )
        except GoogleAPIError as e:
            # Treat 404/410 as already-deleted
            status = e.details.get("status")
            if status in (404, 410):
                logger.info("Event %s already gone from Google", google_event_id)
                return
            raise

    await loop.run_in_executor(None, _delete)
