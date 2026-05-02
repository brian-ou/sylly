"""Claude-backed chat agent for studying and scheduling.

Builds a system prompt from the user's calendar + courses, calls Claude with
the running conversation, and parses a structured `<response>...</response>`
JSON envelope so the frontend can show a natural-language reply alongside any
proposed calendar events.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from anthropic import Anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.exceptions import ClaudeParseError
from app.models.event import Event, EventType
from app.models.study_concept import StudyConcept
from app.models.syllabus import Syllabus
from app.models.user import User
from app.schemas.chat import (
    ChatContext,
    ChatMessage,
    ChatProposedEvent,
    ChatRole,
)

logger = logging.getLogger(__name__)


_RESPONSE_TAG_RE = re.compile(r"<response>(.*?)</response>", re.DOTALL | re.IGNORECASE)


def _build_client() -> Anthropic:
    settings = get_settings()
    return Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_event_line(ev: Event, syllabus_label: Optional[str]) -> str:
    """Render one event for the system prompt context block."""
    when = ev.start_datetime.isoformat() if ev.start_datetime else "?"
    label = syllabus_label or "(no course)"
    return (
        f"- {ev.title} | {when} | type={ev.event_type.value} | course={label}"
    )


def _format_concept_line(c: StudyConcept, syllabus_label: Optional[str]) -> str:
    mastery = float(c.mastery_score or 0)
    label = syllabus_label or "(no course)"
    return (
        f"- {c.title} | mastery={mastery:.2f} "
        f"| seen={c.times_seen} correct={c.times_correct} | course={label}"
    )


def _build_system_prompt(
    user: User,
    upcoming_events: List[Event],
    syllabi: List[Syllabus],
    study_concepts: List[StudyConcept],
) -> str:
    """Assemble the system prompt with the user's schedule + course context."""
    syllabus_by_id = {s.id: s for s in syllabi}

    course_lines: List[str] = []
    for s in syllabi:
        bits: List[str] = []
        if s.course_code:
            bits.append(s.course_code)
        if s.course_name:
            bits.append(s.course_name)
        label = " - ".join(bits) if bits else "(unnamed course)"
        if s.term:
            label += f" [{s.term}]"
        if s.grade_categories:
            cats = ", ".join(
                f"{gc.name} {float(gc.weight):.0f}%" for gc in s.grade_categories
            )
            label += f" — grading: {cats}"
        course_lines.append(f"- {label}")

    upcoming_lines: List[str] = []
    exam_lines: List[str] = []
    for ev in upcoming_events[:30]:
        syllabus_label: Optional[str] = None
        if ev.syllabus_id and ev.syllabus_id in syllabus_by_id:
            s = syllabus_by_id[ev.syllabus_id]
            syllabus_label = s.course_code or s.course_name
        upcoming_lines.append(_format_event_line(ev, syllabus_label))
        if ev.event_type == EventType.EXAM:
            exam_lines.append(_format_event_line(ev, syllabus_label))

    # Show the weakest 8 concepts so the assistant knows where to push active
    # recall, plus a count of mastered ones so it can celebrate progress.
    sorted_concepts = sorted(
        study_concepts, key=lambda c: float(c.mastery_score or 0)
    )
    weak_concepts = sorted_concepts[:8]
    mastered_count = sum(
        1 for c in study_concepts if float(c.mastery_score or 0) >= 0.8
    )
    weak_concept_lines: List[str] = []
    for c in weak_concepts:
        sl: Optional[str] = None
        if c.syllabus_id and c.syllabus_id in syllabus_by_id:
            s = syllabus_by_id[c.syllabus_id]
            sl = s.course_code or s.course_name
        weak_concept_lines.append(_format_concept_line(c, sl))

    courses_block = "\n".join(course_lines) if course_lines else "(none on file)"
    upcoming_block = (
        "\n".join(upcoming_lines) if upcoming_lines else "(no upcoming events)"
    )
    exams_block = "\n".join(exam_lines) if exam_lines else "(no upcoming exams)"
    if study_concepts:
        study_block = (
            f"Total concepts: {len(study_concepts)} | mastered (>=0.80): "
            f"{mastered_count}\nWeakest concepts to focus on:\n"
            + ("\n".join(weak_concept_lines) if weak_concept_lines else "(none)")
        )
    else:
        study_block = (
            "(no study concepts yet — invite the student to paste course "
            "material into the study tool to start active recall)"
        )

    settings = get_settings()
    default_tz = settings.DEFAULT_TIMEZONE or "America/Los_Angeles"

    return f"""You are a study and scheduling assistant for a college student. \
You help with planning study time, organizing assignments, and active-recall \
practice on course material.

The student's enrolled courses:
{courses_block}

The student's upcoming events (next ~30):
{upcoming_block}

Upcoming exams specifically (factor exam proximity into study suggestions — \
weight closer exams more, and prioritize study sessions ahead of them):
{exams_block}

The student's study-tool progress (active recall the backend already tracks):
{study_block}

When weak concepts exist for an upcoming exam, prefer quizzing on those over \
inventing new material. Reference the concept by name. The student can also \
paste new course material into the study tool — suggest that when they ask \
"how do I study X" and there are no concepts on file for X.

You can do TWO things:

1. Respond conversationally. This includes general questions, motivation, \
explaining concepts, and ACTIVE RECALL practice: quiz the student on \
concepts from a course, ask them questions one at a time, and give feedback. \
Infer concepts from the course name and upcoming exams. Use the Socratic \
method — guide with questions, don't lecture. Praise effort and accuracy \
specifically (e.g. "good — you correctly identified X" rather than just \
"great job"). When the student gets something wrong, prompt them toward the \
right answer instead of dumping the answer.

2. Propose new calendar events when the student asks to schedule study time, \
plan a session, or set reminders. Return 1-3 concrete proposals. Each \
proposed event's start_datetime MUST be a real ISO 8601 datetime. Use the \
student's local timezone (default {default_tz}) when they don't specify one. \
Avoid scheduling on top of existing events shown above.

When the student is just chatting, quizzing, or asking general questions, \
omit `proposed_events` or return an empty array.

OUTPUT RULE — you must respond in this exact format, with the JSON wrapped \
in <response>...</response> tags and nothing else outside the tags:

<response>
{{
  "message": "the natural-language response shown to the user",
  "proposed_events": [
    {{
      "title": "string",
      "start_datetime": "ISO 8601 datetime",
      "end_datetime": "ISO 8601 datetime or null",
      "event_type": "assignment|exam|lecture|holiday|office_hours|other",
      "description": "string or null"
    }}
  ]
}}
</response>

`proposed_events` may be `[]` or omitted entirely when no events are being \
proposed. The `message` field is required and must be a non-empty string."""


async def _fetch_context(
    user: User,
    context: Optional[ChatContext],
    db: AsyncSession,
) -> Tuple[List[Event], List[Syllabus], List[StudyConcept]]:
    """Load the events overlapping the visible range (or next 30 days),
    the user's syllabi with grade categories, and their study concepts.
    """
    if context and context.visible_range:
        range_start = context.visible_range.start
        range_end = context.visible_range.end
    else:
        range_start = datetime.now(timezone.utc)
        range_end = range_start + timedelta(days=30)

    # Localize naive datetimes if the client somehow sent them (defensive —
    # FastAPI will usually parse with tzinfo, but pydantic accepts naive too).
    if range_start.tzinfo is None:
        range_start = range_start.replace(tzinfo=timezone.utc)
    if range_end.tzinfo is None:
        range_end = range_end.replace(tzinfo=timezone.utc)

    events_stmt = (
        select(Event)
        .where(
            Event.user_id == user.id,
            Event.start_datetime >= range_start,
            Event.start_datetime < range_end,
        )
        .order_by(Event.start_datetime.asc())
    )
    events = list((await db.execute(events_stmt)).scalars().all())

    syllabi_stmt = (
        select(Syllabus)
        .options(selectinload(Syllabus.grade_categories))
        .where(Syllabus.user_id == user.id)
        .order_by(Syllabus.created_at.desc())
    )
    syllabi = list((await db.execute(syllabi_stmt)).scalars().all())

    # Cap at 60: enough to summarize even a heavy study set without bloating
    # the system prompt. The agent only renders the weakest 8 anyway.
    concepts_stmt = (
        select(StudyConcept)
        .where(StudyConcept.user_id == user.id)
        .order_by(StudyConcept.mastery_score.asc())
        .limit(60)
    )
    study_concepts = list((await db.execute(concepts_stmt)).scalars().all())

    return events, syllabi, study_concepts


def _to_anthropic_messages(messages: List[ChatMessage]) -> List[dict]:
    """Convert our ChatMessage list into Anthropic's message format.

    System role messages are dropped here (the system prompt is passed
    separately via the `system` parameter).
    """
    out: List[dict] = []
    for m in messages:
        if m.role == ChatRole.SYSTEM:
            continue
        role = "user" if m.role == ChatRole.USER else "assistant"
        out.append({"role": role, "content": m.content})
    return out


def _extract_response_envelope(raw_text: str) -> Tuple[str, List[dict]]:
    """Pull `message` + `proposed_events` out of the <response>...</response>
    JSON envelope. Falls back to treating the whole text as a plain message.
    """
    match = _RESPONSE_TAG_RE.search(raw_text)
    if not match:
        logger.warning("Chat response missing <response> tags; falling back")
        return raw_text.strip(), []

    payload = match.group(1).strip()
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        logger.warning("Chat response envelope was not valid JSON; falling back")
        return raw_text.strip(), []

    if not isinstance(data, dict):
        return raw_text.strip(), []

    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        return raw_text.strip(), []

    raw_proposals = data.get("proposed_events") or []
    if not isinstance(raw_proposals, list):
        raw_proposals = []

    return message.strip(), raw_proposals


def _coerce_proposals(raw_proposals: List[dict]) -> List[ChatProposedEvent]:
    """Validate raw proposal dicts into ChatProposedEvent instances.

    Skips malformed entries rather than raising — the assistant's reply is
    still useful even if one proposed slot is unparseable.
    """
    valid_types = {e.value for e in EventType}
    out: List[ChatProposedEvent] = []
    for raw in raw_proposals:
        if not isinstance(raw, dict):
            continue
        try:
            event_type = raw.get("event_type") or "other"
            if event_type not in valid_types:
                event_type = "other"
            proposal = ChatProposedEvent(
                proposal_id=str(uuid.uuid4()),
                title=str(raw["title"]),
                start_datetime=raw["start_datetime"],
                end_datetime=raw.get("end_datetime"),
                event_type=event_type,
                description=raw.get("description"),
            )
        except Exception as e:  # noqa: BLE001 - skip individual bad entries
            logger.warning("Skipping malformed proposed event: %s", e)
            continue
        out.append(proposal)
    return out


async def chat_plan(
    user: User,
    messages: List[ChatMessage],
    context: Optional[ChatContext],
    db: AsyncSession,
) -> Tuple[ChatMessage, List[ChatProposedEvent]]:
    """Run a single chat turn against Claude and parse out any event proposals.

    Returns the assistant's ChatMessage plus a list of proposed events (which
    may be empty). Raises `ClaudeParseError` on API failures.
    """
    settings = get_settings()
    client = _build_client()

    upcoming_events, syllabi, study_concepts = await _fetch_context(
        user, context, db
    )
    system_prompt = _build_system_prompt(
        user, upcoming_events, syllabi, study_concepts
    )
    api_messages = _to_anthropic_messages(messages)

    if not api_messages:
        # Anthropic requires at least one message. Caller should have validated
        # this, but guard defensively so we never send an empty payload.
        raise ClaudeParseError("No chat messages to send to the model")

    try:
        response = client.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=2048,
            system=system_prompt,
            messages=api_messages,
        )
    except Exception as e:
        logger.exception("Anthropic chat call failed")
        raise ClaudeParseError(f"Anthropic chat call failed: {e}") from e

    try:
        usage = getattr(response, "usage", None)
        in_tokens = getattr(usage, "input_tokens", None)
        out_tokens = getattr(usage, "output_tokens", None)
        logger.info(
            "Chat plan model=%s input_tokens=%s output_tokens=%s",
            settings.ANTHROPIC_MODEL,
            in_tokens,
            out_tokens,
        )
    except Exception:
        pass

    text_chunks: List[str] = []
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
        logger.error("Claude chat returned empty response")
        raise ClaudeParseError("Claude returned an empty response")

    message_text, raw_proposals = _extract_response_envelope(raw_text)
    proposals = _coerce_proposals(raw_proposals)

    assistant_message = ChatMessage(
        id=str(uuid.uuid4()),
        role=ChatRole.ASSISTANT,
        content=message_text,
        created_at=_now_iso(),
        pending=None,
    )
    return assistant_message, proposals
