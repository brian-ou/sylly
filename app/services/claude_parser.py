"""Claude-based PDF syllabus parser."""
from __future__ import annotations

import base64
import json
import logging
import time
from datetime import date
from typing import Any, Optional

from anthropic import Anthropic

from app.config import get_settings
from app.exceptions import ClaudeParseError
from app.schemas.syllabus import ParsedSyllabus

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You extract dated academic events AND the grading scheme from course "
    "syllabi. You return ONLY valid JSON matching the schema provided — no "
    "preamble, no markdown fences, no explanation. If a field is unknown, "
    "use null. If you are uncertain about a date, set confidence to \"low\"."
)

OUTPUT_SCHEMA = """{
  "course_name": "string or null",
  "course_code": "string or null",
  "term": "string or null",
  "timezone": "IANA timezone string or null",
  "events": [
    {
      "title": "string",
      "description": "string or null",
      "start_datetime": "ISO 8601 string",
      "end_datetime": "ISO 8601 string or null",
      "is_all_day": true,
      "recurrence_rule": "RRULE string or null",
      "event_type": "assignment|exam|lecture|holiday|office_hours|other",
      "confidence": "high|medium|low"
    }
  ],
  "grade_categories": [
    {
      "name": "string (e.g. Homework, Midterm, Final, Participation)",
      "weight": "number 0-100 (percentage points)",
      "drop_lowest": "integer >= 0 (how many of the lowest scores are dropped, 0 if none)",
      "notes": "string or null (any extra rule, e.g. 'must pass final to pass course')"
    }
  ]
}"""

EXTRACTION_INSTRUCTIONS = """Extraction instructions:
- Identify course name, course code, and term.
- Extract every dated item: lectures, assignments, readings, exams, project milestones, holidays, office hours, drop deadlines.
- For recurring events (e.g. "MWF lectures Aug 26 - Dec 5"), output ONE event with a recurrence_rule in RFC 5545 RRULE format, not many individual events.
- IMPORTANT: For regularly scheduled class meetings (lectures, sections, labs, recitations), output ONE recurring event per pattern even when the syllabus lists each session's topic separately. Use a generic title like "Lecture", "Section", or "Lab" — do NOT create one event per session for each weekly topic. Per-session topics belong in a syllabus, not on a calendar. This rule does not apply to exams, assignments, or one-off events, which should still be listed individually.
- For events without an explicit year, infer it from the term/syllabus header.
- Use ISO 8601 for dates and times. Use the syllabus's stated timezone if given, otherwise leave as a naive datetime and the backend will assume America/Los_Angeles.
- Set is_all_day: true for assignments without a specific time.
- Use these event_type values exactly: assignment, exam, lecture, holiday, office_hours, other.
- Set confidence: "low" when the date is ambiguous, the year is inferred, or the time is unclear.
- Look for a "Grading", "Evaluation", "Assessment", or "Course policies" section
  and emit one entry per weighted bucket in `grade_categories`. Use the
  syllabus's own labels (e.g. "Homework", "Problem Sets", "Midterm",
  "Final Exam", "Participation"). Express weight as a number in 0-100; a
  syllabus that says "30%" becomes weight: 30. If the syllabus mentions
  dropping the lowest N scores in a bucket, set drop_lowest to N. Put any
  qualifying language (e.g. "must pass exam to pass course") in notes.
- If no grading scheme is present, return an empty array — do NOT invent
  weights.
"""


def _build_client() -> Anthropic:
    settings = get_settings()
    return Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences if Claude returned them despite instructions."""
    text = text.strip()
    if text.startswith("```"):
        # remove opening fence (possibly ```json)
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def parse_syllabus_pdf(
    pdf_bytes: bytes,
    course_hint: Optional[str] = None,
    term_hint: Optional[str] = None,
    today: Optional[date] = None,
    client: Optional[Anthropic] = None,
) -> ParsedSyllabus:
    """Send a PDF to Claude and return a validated ParsedSyllabus.

    Args:
        pdf_bytes: Raw PDF bytes.
        course_hint: Optional user-provided course name hint.
        term_hint: Optional user-provided term hint (e.g., "Fall 2026").
        today: Override for today's date (used in tests).
        client: Optional Anthropic client (used in tests for mocking).
    """
    settings = get_settings()
    client = client or _build_client()
    today = today or date.today()

    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    hints_text = f"Today's date is {today.isoformat()}."
    if course_hint:
        hints_text += f" Course hint: {course_hint}."
    if term_hint:
        hints_text += f" Term hint: {term_hint}."

    instructions_text = (
        f"{EXTRACTION_INSTRUCTIONS}\n\n"
        f"Output schema (return JSON matching this exactly):\n{OUTPUT_SCHEMA}"
    )

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_b64,
                    },
                },
                {"type": "text", "text": hints_text},
                {"type": "text", "text": instructions_text},
            ],
        }
    ]

    started = time.monotonic()
    try:
        response = client.messages.create(
            model=settings.ANTHROPIC_MODEL,
            # Long syllabi (e.g., a full semester of dated readings + assignments
            # + recurring lectures + exams) can easily exceed 4K output tokens.
            # 8192 is supported by all current Sonnet and Opus models without
            # any extended-output beta header.
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
    except Exception as e:
        logger.exception("Anthropic API call failed")
        raise ClaudeParseError(f"Anthropic API call failed: {e}") from e
    latency_ms = int((time.monotonic() - started) * 1000)

    # Log usage but never the content
    try:
        usage = getattr(response, "usage", None)
        in_tokens = getattr(usage, "input_tokens", None)
        out_tokens = getattr(usage, "output_tokens", None)
        logger.info(
            "Claude parse model=%s input_tokens=%s output_tokens=%s latency_ms=%s",
            settings.ANTHROPIC_MODEL,
            in_tokens,
            out_tokens,
            latency_ms,
        )
    except Exception:
        pass

    text_chunks = []
    for block in getattr(response, "content", []) or []:
        block_type = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if block_type == "text":
            chunk = getattr(block, "text", None) or (
                block.get("text") if isinstance(block, dict) else ""
            )
            if chunk:
                text_chunks.append(chunk)

    raw_text = "\n".join(text_chunks).strip()
    if not raw_text:
        logger.error("Claude returned empty response")
        raise ClaudeParseError("Claude returned an empty response")

    cleaned = _strip_code_fences(raw_text)

    try:
        data: Any = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error("Claude returned non-JSON response: %s", cleaned[:500])
        raise ClaudeParseError(f"Claude returned non-JSON output: {e}") from e

    try:
        parsed = ParsedSyllabus.model_validate(data)
    except Exception as e:
        logger.error("Claude output failed schema validation: %s", e)
        raise ClaudeParseError(f"Claude output did not match schema: {e}") from e

    # Sanitize grade_categories: drop blank names and dedupe case-insensitively
    # so we don't violate the unique (syllabus_id, lower(name)) index when we
    # persist them. First-occurrence wins.
    seen: set[str] = set()
    cleaned_categories = []
    for gc in parsed.grade_categories:
        if not gc.name:
            continue
        key = gc.name.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned_categories.append(gc)
    parsed.grade_categories = cleaned_categories

    return parsed
