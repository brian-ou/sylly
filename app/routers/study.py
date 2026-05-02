"""Study tool routes: ingest material, run quiz cycles, view progress."""
from __future__ import annotations

import io
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user
from app.exceptions import (
    ClaudeParseError,
    InvalidInputError,
    NotFoundError,
    PDFTooLargeError,
    RateLimitExceededError,
)
from app.models.event import Event, EventType
from app.models.study_attempt import StudyAttempt
from app.models.study_concept import StudyConcept
from app.models.syllabus import Syllabus
from app.models.user import User
from app.schemas.chat import ChatMessage, ChatRole
from app.schemas.study import (
    QuizGradeRequest,
    QuizGradeResponse,
    QuizNextRequest,
    QuizQuestion,
    StudyChatRequest,
    StudyChatResponse,
    StudyConceptRead,
    StudyMaterialIngestRequest,
    StudyMaterialIngestResponse,
    StudyProgressBucket,
    StudyProgressResponse,
)
from app.services.claude_parser import extract_pdf_text
from app.services.rate_limit import SlidingWindowRateLimiter
from app.services.study_agent import (
    CORRECT_THRESHOLD,
    apply_attempt_to_concept,
    extract_concepts,
    generate_question,
    grade_answer,
    study_chat,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["study"])


# Tighter limit than chat — concept extraction is the most expensive call here
# and a paste of a textbook chapter shouldn't cost 60 ingests/hr.
study_ingest_limiter = SlidingWindowRateLimiter(limit=20, window_seconds=3600)
# Quiz cycle (next + grade) is per-call cheap; allow a real study session.
study_quiz_limiter = SlidingWindowRateLimiter(limit=240, window_seconds=3600)
# Tutor chat: matches the general chat limit so a long study session isn't
# arbitrarily cut off.
study_chat_limiter = SlidingWindowRateLimiter(limit=120, window_seconds=3600)


def _to_read(concept: StudyConcept) -> StudyConceptRead:
    """Hand-rolled adapter so we can coerce the Numeric mastery to a float."""
    return StudyConceptRead(
        id=concept.id,
        syllabus_id=concept.syllabus_id,
        title=concept.title,
        summary=concept.summary,
        mastery_score=float(concept.mastery_score or 0),
        times_seen=concept.times_seen or 0,
        times_correct=concept.times_correct or 0,
        last_attempted_at=concept.last_attempted_at,
        created_at=concept.created_at,
    )


async def _own_syllabus_or_404(
    syllabus_id: uuid.UUID, user: User, db: AsyncSession
) -> Syllabus:
    stmt = select(Syllabus).where(
        Syllabus.id == syllabus_id, Syllabus.user_id == user.id
    )
    s = (await db.execute(stmt)).scalar_one_or_none()
    if s is None:
        raise NotFoundError("Syllabus not found")
    return s


async def _own_concept_or_404(
    concept_id: uuid.UUID, user: User, db: AsyncSession
) -> StudyConcept:
    stmt = select(StudyConcept).where(
        StudyConcept.id == concept_id, StudyConcept.user_id == user.id
    )
    c = (await db.execute(stmt)).scalar_one_or_none()
    if c is None:
        raise NotFoundError("Concept not found")
    return c


async def _persist_extracted_concepts(
    user: User,
    db: AsyncSession,
    syllabus_id: Optional[uuid.UUID],
    material: str,
    max_concepts: int,
) -> List[StudyConcept]:
    """Run extract_concepts + dedupe + persist. Shared by JSON + PDF ingest."""
    extracted = extract_concepts(material=material, max_concepts=max_concepts)
    if not extracted:
        return []

    existing_stmt = select(StudyConcept).where(
        StudyConcept.user_id == user.id,
        StudyConcept.syllabus_id == syllabus_id,
    )
    existing = list((await db.execute(existing_stmt)).scalars().all())
    existing_titles = {c.title.lower() for c in existing}

    saved: List[StudyConcept] = []
    for ec in extracted:
        if ec.title.lower() in existing_titles:
            continue
        existing_titles.add(ec.title.lower())
        concept = StudyConcept(
            user_id=user.id,
            syllabus_id=syllabus_id,
            title=ec.title,
            summary=ec.summary,
            source_text=material[:8000],
        )
        db.add(concept)
        saved.append(concept)

    await db.commit()
    for c in saved:
        await db.refresh(c)
    return saved


@router.post(
    "/study/material",
    response_model=StudyMaterialIngestResponse,
    summary="Ingest pasted course material into atomic study concepts",
)
async def ingest_material(
    body: StudyMaterialIngestRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StudyMaterialIngestResponse:
    """Extract concepts from pasted material and persist them.

    Concepts are deduped against the user's existing concepts (within the same
    syllabus scope) by case-insensitive title — re-ingesting the same chapter
    won't double up the study set.
    """
    if not study_ingest_limiter.check(str(current_user.id)):
        raise RateLimitExceededError("Limit is 20 study ingests per hour")

    if body.syllabus_id is not None:
        await _own_syllabus_or_404(body.syllabus_id, current_user, db)

    saved = await _persist_extracted_concepts(
        user=current_user,
        db=db,
        syllabus_id=body.syllabus_id,
        material=body.material,
        max_concepts=body.max_concepts,
    )
    return StudyMaterialIngestResponse(
        syllabus_id=body.syllabus_id,
        concepts=[_to_read(c) for c in saved],
    )


@router.post(
    "/study/material/pdf",
    response_model=StudyMaterialIngestResponse,
    summary="Upload a PDF (notes, chapter, slides) and extract study concepts",
)
async def ingest_material_pdf(
    file: UploadFile = File(..., description="PDF (<=20MB, <=100 pages)"),
    syllabus_id: Optional[uuid.UUID] = Form(default=None),
    max_concepts: int = Form(default=12, ge=1, le=40),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StudyMaterialIngestResponse:
    """Same as `/study/material` but accepts a PDF.

    Text is extracted locally with pypdf (no model call needed for the
    extraction step), then run through the same concept extractor. Image-only
    PDFs that yield no text are rejected with a clear error rather than
    silently sending the whole binary to the model.
    """
    settings = get_settings()

    if not study_ingest_limiter.check(str(current_user.id)):
        raise RateLimitExceededError("Limit is 20 study ingests per hour")

    if file.content_type != "application/pdf":
        raise InvalidInputError("File must be application/pdf")

    if syllabus_id is not None:
        await _own_syllabus_or_404(syllabus_id, current_user, db)

    pdf_bytes = await file.read()
    if len(pdf_bytes) > settings.MAX_PDF_BYTES:
        raise PDFTooLargeError(
            f"PDF exceeds maximum size of {settings.MAX_PDF_BYTES} bytes"
        )

    try:
        reader = PdfReader(stream=io.BytesIO(pdf_bytes))
        page_count = len(reader.pages)
    except PdfReadError as e:
        raise InvalidInputError(f"Could not read PDF: {e}") from e
    except Exception as e:  # pypdf can raise non-PdfReadError on weird files
        raise InvalidInputError(f"Could not read PDF: {e}") from e

    if page_count > settings.MAX_PDF_PAGES:
        raise PDFTooLargeError(
            f"PDF has {page_count} pages; max is {settings.MAX_PDF_PAGES}"
        )

    text = extract_pdf_text(pdf_bytes)
    if len(text) < settings.PARSE_TEXT_MIN_CHARS:
        raise InvalidInputError(
            "Couldn't extract enough text from the PDF — it may be scanned or "
            "image-based. Try pasting the text in directly."
        )

    saved = await _persist_extracted_concepts(
        user=current_user,
        db=db,
        syllabus_id=syllabus_id,
        material=text,
        max_concepts=max_concepts,
    )
    return StudyMaterialIngestResponse(
        syllabus_id=syllabus_id,
        concepts=[_to_read(c) for c in saved],
    )


@router.get(
    "/study/concepts",
    response_model=List[StudyConceptRead],
    summary="List the current user's study concepts",
)
async def list_concepts(
    syllabus_id: Optional[uuid.UUID] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[StudyConceptRead]:
    """Return concepts ordered by lowest mastery first (focus on weak spots)."""
    stmt = select(StudyConcept).where(StudyConcept.user_id == current_user.id)
    if syllabus_id is not None:
        stmt = stmt.where(StudyConcept.syllabus_id == syllabus_id)
    stmt = stmt.order_by(
        StudyConcept.mastery_score.asc(), StudyConcept.created_at.desc()
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return [_to_read(c) for c in rows]


@router.delete(
    "/study/concepts/{concept_id}",
    status_code=204,
    summary="Delete a study concept",
)
async def delete_concept(
    concept_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a single study concept and all its attempts."""
    concept = await _own_concept_or_404(concept_id, current_user, db)
    await db.delete(concept)
    await db.commit()


def _pick_concept(
    concepts: List[StudyConcept],
    next_exam_at: Optional[datetime],
) -> StudyConcept:
    """Weighted random pick favoring weak mastery and exam proximity.

    Weight = (1 - mastery)^2 + small spaced-repetition boost for concepts not
    seen recently. If `next_exam_at` is within 14 days the weak-mastery weight
    is doubled — closer exams crowd out exploration.
    """
    now = datetime.now(timezone.utc)
    exam_close = (
        next_exam_at is not None
        and (next_exam_at - now) < timedelta(days=14)
    )
    weights: List[float] = []
    for c in concepts:
        mastery = float(c.mastery_score or 0)
        base = (1 - mastery) ** 2 + 0.05  # never zero, so new concepts can hit
        if exam_close:
            base *= 2.0
        if c.last_attempted_at is None:
            base += 0.5  # nudge brand-new concepts toward being asked first
        else:
            hours_since = (
                now - c.last_attempted_at.astimezone(timezone.utc)
            ).total_seconds() / 3600
            base += min(hours_since / 48.0, 0.5)  # cap the recency boost
        weights.append(base)
    return random.choices(concepts, weights=weights, k=1)[0]


@router.post(
    "/study/quiz/next",
    response_model=QuizQuestion,
    summary="Get the next active-recall question",
)
async def quiz_next(
    body: QuizNextRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QuizQuestion:
    """Pick a concept (weighted by weakness + exam proximity) and ask about it.

    If `concept_id` is given, use that one; otherwise the picker chooses from
    the syllabus-scoped or full pool. Returns 404 if there are no concepts to
    quiz on yet.
    """
    if not study_quiz_limiter.check(str(current_user.id)):
        raise RateLimitExceededError("Limit is 240 quiz calls per hour")

    if body.concept_id is not None:
        concept = await _own_concept_or_404(body.concept_id, current_user, db)
    else:
        stmt = select(StudyConcept).where(StudyConcept.user_id == current_user.id)
        if body.syllabus_id is not None:
            stmt = stmt.where(StudyConcept.syllabus_id == body.syllabus_id)
        pool = list((await db.execute(stmt)).scalars().all())
        if not pool:
            raise NotFoundError(
                "No study concepts yet — paste material into /study/material first"
            )

        # Find the next exam in the same syllabus (or any exam if unscoped) so
        # the picker can weight by exam proximity.
        exam_stmt = (
            select(Event.start_datetime)
            .where(
                Event.user_id == current_user.id,
                Event.event_type == EventType.EXAM,
                Event.start_datetime >= datetime.now(timezone.utc),
            )
            .order_by(Event.start_datetime.asc())
            .limit(1)
        )
        if body.syllabus_id is not None:
            exam_stmt = exam_stmt.where(Event.syllabus_id == body.syllabus_id)
        next_exam = (await db.execute(exam_stmt)).scalar_one_or_none()

        concept = _pick_concept(pool, next_exam)

    recent_stmt = (
        select(StudyAttempt)
        .where(StudyAttempt.concept_id == concept.id)
        .order_by(StudyAttempt.attempted_at.desc())
        .limit(5)
    )
    recent = list((await db.execute(recent_stmt)).scalars().all())

    question = generate_question(concept, recent)
    return QuizQuestion(
        concept_id=concept.id,
        concept_title=concept.title,
        question=question,
    )


@router.post(
    "/study/quiz/grade",
    response_model=QuizGradeResponse,
    summary="Grade an answer and update concept mastery",
)
async def quiz_grade(
    body: QuizGradeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QuizGradeResponse:
    """Grade a free-text answer, persist the attempt, update mastery."""
    if not study_quiz_limiter.check(str(current_user.id)):
        raise RateLimitExceededError("Limit is 240 quiz calls per hour")

    concept = await _own_concept_or_404(body.concept_id, current_user, db)

    if not body.question.strip():
        raise InvalidInputError("question must not be empty")

    score, feedback = grade_answer(concept, body.question, body.answer)

    attempted_at = datetime.now(timezone.utc)
    attempt = StudyAttempt(
        user_id=current_user.id,
        concept_id=concept.id,
        question=body.question,
        user_answer=body.answer,
        correct=score >= CORRECT_THRESHOLD,
        score=round(score, 3),
        feedback=feedback,
        attempted_at=attempted_at,
    )
    db.add(attempt)
    apply_attempt_to_concept(concept, score, attempted_at)

    await db.commit()
    await db.refresh(concept)

    return QuizGradeResponse(
        correct=attempt.correct,
        score=score,
        feedback=feedback,
        concept=_to_read(concept),
    )


@router.get(
    "/study/progress",
    response_model=StudyProgressResponse,
    summary="Per-syllabus mastery summary",
)
async def study_progress(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StudyProgressResponse:
    """Aggregate mastery + attempts per syllabus, surfacing the next exam.

    Concepts with no syllabus link land in a single `syllabus_id=None` bucket.
    """
    concepts_stmt = select(StudyConcept).where(
        StudyConcept.user_id == current_user.id
    )
    concepts = list((await db.execute(concepts_stmt)).scalars().all())

    syllabi_stmt = select(Syllabus).where(Syllabus.user_id == current_user.id)
    syllabi = {s.id: s for s in (await db.execute(syllabi_stmt)).scalars().all()}

    now = datetime.now(timezone.utc)
    exam_stmt = (
        select(Event.syllabus_id, func.min(Event.start_datetime))
        .where(
            Event.user_id == current_user.id,
            Event.event_type == EventType.EXAM,
            Event.start_datetime >= now,
        )
        .group_by(Event.syllabus_id)
    )
    next_exam_by_syllabus = {
        row[0]: row[1] for row in (await db.execute(exam_stmt)).all()
    }

    attempts_stmt = (
        select(StudyAttempt.concept_id, func.count())
        .where(StudyAttempt.user_id == current_user.id)
        .group_by(StudyAttempt.concept_id)
    )
    attempts_by_concept = {
        row[0]: row[1] for row in (await db.execute(attempts_stmt)).all()
    }

    buckets: dict[Optional[uuid.UUID], List[StudyConcept]] = {}
    for c in concepts:
        buckets.setdefault(c.syllabus_id, []).append(c)

    out: List[StudyProgressBucket] = []
    for syllabus_id, items in buckets.items():
        mastered = sum(1 for c in items if float(c.mastery_score or 0) >= 0.8)
        struggling = sum(
            1
            for c in items
            if (c.times_seen or 0) >= 2 and float(c.mastery_score or 0) < 0.4
        )
        total_attempts = sum(
            attempts_by_concept.get(c.id, 0) for c in items
        )
        avg = (
            sum(float(c.mastery_score or 0) for c in items) / len(items)
            if items
            else 0.0
        )
        course_label: Optional[str] = None
        if syllabus_id is not None and syllabus_id in syllabi:
            s = syllabi[syllabus_id]
            course_label = s.course_code or s.course_name
        out.append(
            StudyProgressBucket(
                syllabus_id=syllabus_id,
                course_label=course_label,
                concept_count=len(items),
                mastered_count=mastered,
                struggling_count=struggling,
                total_attempts=total_attempts,
                average_mastery=round(avg, 3),
                next_exam_at=next_exam_by_syllabus.get(syllabus_id),
            )
        )
    # Sort by exam proximity. SQLite (used in tests) drops tz-info, so
    # localize naive values to UTC before comparing — otherwise a mixed bucket
    # set would crash on `<` comparison.
    def _sort_key(b: StudyProgressBucket) -> datetime:
        when = b.next_exam_at
        if when is None:
            return datetime.max.replace(tzinfo=timezone.utc)
        if when.tzinfo is None:
            return when.replace(tzinfo=timezone.utc)
        return when

    out.sort(key=_sort_key)
    return StudyProgressResponse(buckets=out)


@router.post(
    "/study/chat",
    response_model=StudyChatResponse,
    summary="Conversational study tutor with mastery tracking",
)
async def study_chat_endpoint(
    body: StudyChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StudyChatResponse:
    """Run one Socratic-tutor turn against the user's study set.

    The frontend re-sends the full conversation each turn (stateless server).
    The tutor may emit `assessments` inline when the student demonstrates
    clear understanding or a clear gap; each assessment is persisted as a
    `study_attempts` row and updates the concept's mastery via the same EMA
    used by the explicit grader. The updated concept rows come back in
    `updated_concepts` so the UI can show the bump immediately.
    """
    if not study_chat_limiter.check(str(current_user.id)):
        raise RateLimitExceededError("Limit is 120 study chat calls per hour")

    if not body.messages:
        raise InvalidInputError("messages must not be empty")
    if body.messages[-1].role != ChatRole.USER:
        raise InvalidInputError("last message must be from the user")

    if body.syllabus_id is not None:
        await _own_syllabus_or_404(body.syllabus_id, current_user, db)

    focus_concept: Optional[StudyConcept] = None
    if body.concept_id is not None:
        focus_concept = await _own_concept_or_404(
            body.concept_id, current_user, db
        )

    # Build the candidate pool: syllabus-scoped if provided, else everything
    # the user has. Order by weakest first so the tutor sees gaps first.
    pool_stmt = select(StudyConcept).where(StudyConcept.user_id == current_user.id)
    if body.syllabus_id is not None:
        pool_stmt = pool_stmt.where(StudyConcept.syllabus_id == body.syllabus_id)
    pool_stmt = pool_stmt.order_by(StudyConcept.mastery_score.asc()).limit(40)
    pool_concepts = list((await db.execute(pool_stmt)).scalars().all())

    if not pool_concepts and focus_concept is None:
        raise NotFoundError(
            "No study concepts yet — paste material into /study/material first"
        )

    try:
        message_text, assessments = study_chat(
            messages=body.messages,
            focus_concept=focus_concept,
            pool_concepts=pool_concepts,
        )
    except ClaudeParseError:
        raise

    # Apply each assessment as a StudyAttempt + mastery update. We need the
    # actual ORM rows for `apply_attempt_to_concept`, so build a quick lookup.
    pool_by_id = {c.id: c for c in pool_concepts}
    if focus_concept is not None:
        pool_by_id[focus_concept.id] = focus_concept

    updated: List[StudyConcept] = []
    attempted_at = datetime.now(timezone.utc)
    last_user_msg = body.messages[-1].content
    for a in assessments:
        concept = pool_by_id.get(a.concept_id)
        if concept is None:
            # Defense in depth: study_chat already filters unknown ids, but
            # double-check before mutating state.
            continue
        attempt = StudyAttempt(
            user_id=current_user.id,
            concept_id=concept.id,
            # `question` here is "the prompt that elicited the assessment" —
            # we use the last user message as a stand-in since the tutor
            # didn't pose a discrete question/answer pair.
            question=last_user_msg[:2000],
            user_answer=last_user_msg[:2000],
            correct=a.score >= CORRECT_THRESHOLD,
            score=round(a.score, 3),
            feedback=a.note,
            attempted_at=attempted_at,
        )
        db.add(attempt)
        apply_attempt_to_concept(concept, a.score, attempted_at)
        updated.append(concept)

    if updated:
        await db.commit()
        for c in updated:
            await db.refresh(c)

    assistant_message = ChatMessage(
        id=str(uuid.uuid4()),
        role=ChatRole.ASSISTANT,
        content=message_text,
        created_at=attempted_at.isoformat(),
        pending=None,
    )
    return StudyChatResponse(
        message=assistant_message,
        updated_concepts=[_to_read(c) for c in updated],
        assessments=assessments,
    )
