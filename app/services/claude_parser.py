"""Claude-based syllabus parser.

Two paths into the model:

1. **Text-first** — pull text out of the PDF locally with pypdf and send only
   that. Drops input tokens by an order of magnitude on a normal text-based
   syllabus and lets us use the cheaper/faster ANTHROPIC_PARSE_MODEL.
2. **PDF fallback** — when text extraction yields too little (image-only PDFs,
   scans), fall back to base64 PDF document upload.

The output schema and `_strip_code_fences` post-processing are unchanged from
the previous version, so callers and tests don't need to know which path ran.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import time
from datetime import date
from typing import Any, List, Optional

from anthropic import Anthropic
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.config import get_settings
from app.exceptions import ClaudeParseError
from app.schemas.syllabus import ParsedSyllabus

logger = logging.getLogger(__name__)

# Compact system prompt — the previous one repeated the no-fences/no-preamble
# rule across multiple sentences. Same intent, ~half the tokens.
SYSTEM_PROMPT = (
    "Extract dated academic events and the grading scheme from a syllabus. "
    "Return ONLY valid JSON matching the supplied schema. Use null for "
    "unknowns; confidence \"low\" for ambiguous dates."
)

# Single-line schema — Claude does not need pretty-printed JSON, and the saved
# tokens add up across runs. Field semantics are explained in EXTRACTION_RULES
# right below so we don't pay the schema-comment tax twice.
OUTPUT_SCHEMA = (
    '{"course_name":string|null,"course_code":string|null,"term":string|null,'
    '"timezone":string|null,'
    '"events":[{"title":string,"description":string|null,'
    '"start_datetime":ISO8601,"end_datetime":ISO8601|null,'
    '"is_all_day":bool,"recurrence_rule":RRULE|null,'
    '"event_type":"assignment|exam|lecture|holiday|office_hours|other",'
    '"confidence":"high|medium|low"}],'
    '"grade_categories":[{"name":string,"weight":0-100,'
    '"drop_lowest":int>=0,"notes":string|null}]}'
)

EXTRACTION_RULES = (
    "Rules:\n"
    "- Extract every dated item: lectures, assignments, readings, exams, "
    "milestones, holidays, office hours, drop deadlines.\n"
    "- Recurring meetings (e.g. MWF lectures) → ONE event with an RFC 5545 "
    "RRULE. Generic title (\"Lecture\", \"Section\"); per-session topics "
    "belong in a syllabus, not the calendar. Exams and one-offs stay "
    "individual.\n"
    "- Infer year from term/header when missing. ISO 8601 datetimes; naive "
    "OK (backend assumes America/Los_Angeles).\n"
    "- is_all_day:true for assignments without a specific time.\n"
    "- event_type values exactly: assignment|exam|lecture|holiday|"
    "office_hours|other.\n"
    "- confidence:\"low\" when date is ambiguous or year inferred.\n"
    "- grade_categories: one entry per weighted bucket (Homework, Midterm, "
    "Final, Participation, etc.). weight is 0-100 (a syllabus's \"30%\" "
    "becomes 30). drop_lowest: N if syllabus drops N. notes: any qualifier "
    "(e.g. \"must pass final\"). If no scheme: empty array — do NOT invent."
)


def _build_client() -> Anthropic:
    settings = get_settings()
    return Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences if Claude returned them despite instructions."""
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Pull plain text from a PDF using pypdf. Empty string on failure.

    Joins pages with a `\n\n--- page N ---\n` separator so date lookups can
    still anchor on page boundaries (some syllabi date-stamp by section).
    """
    try:
        reader = PdfReader(stream=io.BytesIO(pdf_bytes))
    except PdfReadError as e:
        logger.warning("pypdf could not open PDF for text extraction: %s", e)
        return ""
    except Exception as e:  # noqa: BLE001 - pypdf is noisy on weird files
        logger.warning("pypdf raised on text extraction: %s", e)
        return ""

    chunks: List[str] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("Skipping page %s: extract_text failed: %s", i, e)
            continue
        text = text.strip()
        if text:
            chunks.append(f"--- page {i} ---\n{text}")
    return "\n\n".join(chunks)


def _build_text_messages(text: str, hints: str, instructions: str) -> List[dict]:
    """Single user turn carrying syllabus text + hints + instructions."""
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": hints},
                {"type": "text", "text": instructions},
                {"type": "text", "text": f"--- syllabus text ---\n{text}"},
            ],
        }
    ]


def _build_pdf_messages(pdf_bytes: bytes, hints: str, instructions: str) -> List[dict]:
    """Fallback path: send the PDF as a base64 document block."""
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    return [
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
                {"type": "text", "text": hints},
                {"type": "text", "text": instructions},
            ],
        }
    ]


def parse_syllabus_pdf(
    pdf_bytes: bytes,
    course_hint: Optional[str] = None,
    term_hint: Optional[str] = None,
    today: Optional[date] = None,
    client: Optional[Anthropic] = None,
) -> ParsedSyllabus:
    """Parse a syllabus PDF into a validated ParsedSyllabus.

    Tries local text extraction first (cheap + fast). Falls back to sending
    the PDF document if pypdf yields fewer than `PARSE_TEXT_MIN_CHARS` chars
    (i.e. the PDF is image-based or pypdf failed).

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

    hints_text = f"Today's date is {today.isoformat()}."
    if course_hint:
        hints_text += f" Course hint: {course_hint}."
    if term_hint:
        hints_text += f" Term hint: {term_hint}."

    instructions_text = (
        f"{EXTRACTION_RULES}\n\nOutput schema (return JSON exactly matching):"
        f"\n{OUTPUT_SCHEMA}"
    )

    text = extract_pdf_text(pdf_bytes)
    if len(text) >= settings.PARSE_TEXT_MIN_CHARS:
        messages = _build_text_messages(text, hints_text, instructions_text)
        path = "text"
    else:
        if text:
            logger.info(
                "Extracted only %s chars; falling back to PDF document upload",
                len(text),
            )
        messages = _build_pdf_messages(pdf_bytes, hints_text, instructions_text)
        path = "pdf"

    started = time.monotonic()
    try:
        response = client.messages.create(
            model=settings.ANTHROPIC_PARSE_MODEL,
            # Long syllabi (full semester of dated readings + assignments +
            # recurring lectures + exams) can exceed 4K output tokens. 8192 is
            # supported by Haiku 4.5 / Sonnet / Opus without any beta header.
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
    except Exception as e:
        logger.exception("Anthropic API call failed")
        raise ClaudeParseError(f"Anthropic API call failed: {e}") from e
    latency_ms = int((time.monotonic() - started) * 1000)

    try:
        usage = getattr(response, "usage", None)
        in_tokens = getattr(usage, "input_tokens", None)
        out_tokens = getattr(usage, "output_tokens", None)
        logger.info(
            "Claude parse model=%s path=%s input_tokens=%s output_tokens=%s "
            "latency_ms=%s",
            settings.ANTHROPIC_PARSE_MODEL,
            path,
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
