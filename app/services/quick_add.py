"""Natural-language quick-add: turn a short note into one calendar event.

Powers the dashboard's "Add an event… (e.g., 'Study for econ midterm Tuesday
7pm')" box. Uses the parse-class model (Haiku) since this is a small, bounded
JSON extraction — fast and cheap.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from typing import List, Optional

from anthropic import Anthropic

from app.config import get_settings
from app.exceptions import ClaudeParseError, InvalidInputError
from app.models.event import EventType

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

_SYSTEM = (
    "You convert a short natural-language note into ONE calendar event. "
    "Return ONLY a JSON object — no preamble, no fences — with keys: "
    '"title", "start_datetime" (ISO 8601), "end_datetime" (ISO 8601 or null), '
    '"is_all_day" (bool), "event_type" (one of '
    "assignment|exam|lecture|holiday|office_hours|other), "
    '"description" (string or null).\n'
    "Resolve relative dates ('tomorrow', 'next Tuesday', 'Friday 5pm') against "
    "the given current date. If no time is stated, set is_all_day true and use "
    "midnight local time. Choose the most fitting event_type (a 'midterm' or "
    "'final' is exam; 'study', 'review', homework, problem sets are assignment; "
    "default to other). Keep the title concise and human (strip filler like "
    "'remind me to')."
)


def _build_client() -> Anthropic:
    settings = get_settings()
    return Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def _model_text(response: object) -> str:
    chunks: List[str] = []
    for block in getattr(response, "content", []) or []:
        block_type = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if block_type == "text":
            chunk = getattr(block, "text", None) or (
                block.get("text") if isinstance(block, dict) else ""
            )
            if chunk:
                chunks.append(chunk)
    return "\n".join(chunks).strip()


class ParsedQuickEvent:
    """Lightweight container for the parsed fields (validated downstream)."""

    def __init__(
        self,
        title: str,
        start_datetime: str,
        end_datetime: Optional[str],
        is_all_day: bool,
        event_type: str,
        description: Optional[str],
    ) -> None:
        self.title = title
        self.start_datetime = start_datetime
        self.end_datetime = end_datetime
        self.is_all_day = is_all_day
        self.event_type = event_type
        self.description = description


def parse_quick_event(
    text: str,
    today: Optional[date] = None,
    client: Optional[Anthropic] = None,
) -> ParsedQuickEvent:
    """Parse a natural-language note into a single event's fields.

    Raises InvalidInputError if the text is too vague to yield a title/date,
    ClaudeParseError on API or JSON failure.
    """
    settings = get_settings()
    client = client or _build_client()
    today = today or date.today()

    user_text = (
        f"Current date: {today.isoformat()}.\n"
        f"Note: {text.strip()}\n\n"
        f"Return the JSON event object."
    )

    started = time.monotonic()
    try:
        response = client.messages.create(
            model=settings.ANTHROPIC_PARSE_MODEL,
            max_tokens=400,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_text}],
        )
    except Exception as e:
        logger.exception("Anthropic quick-add call failed")
        raise ClaudeParseError(f"Quick-add parse failed: {e}") from e
    logger.info(
        "Quick-add parse model=%s latency_ms=%s",
        settings.ANTHROPIC_PARSE_MODEL,
        int((time.monotonic() - started) * 1000),
    )

    raw = _strip_fences(_model_text(response))
    if not raw:
        raise ClaudeParseError("Quick-add parser returned empty text")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("Quick-add parser returned non-JSON: %s", raw[:300])
        raise ClaudeParseError(f"Quick-add parser returned non-JSON: {e}") from e
    if not isinstance(data, dict):
        raise ClaudeParseError("Quick-add parser did not return an object")

    title = str(data.get("title") or "").strip()
    start = str(data.get("start_datetime") or "").strip()
    if not title or not start:
        raise InvalidInputError(
            "Couldn't understand that — try including what and when, "
            "e.g. 'Econ problem set due Friday 5pm'."
        )

    valid_types = {e.value for e in EventType}
    event_type = str(data.get("event_type") or "other")
    if event_type not in valid_types:
        event_type = "other"

    end = data.get("end_datetime")
    end_str = str(end).strip() if end else None

    desc = data.get("description")
    desc_str = str(desc).strip() if desc else None

    return ParsedQuickEvent(
        title=title[:1024],
        start_datetime=start,
        end_datetime=end_str or None,
        is_all_day=bool(data.get("is_all_day", False)),
        event_type=event_type,
        description=desc_str or None,
    )
