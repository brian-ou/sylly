"""Claude-backed study tool: extract concepts, generate questions, grade answers.

Three responsibilities, all stateless on the service side:

1. `extract_concepts` — given pasted course material, return a deduped list of
   atomic concepts (title + 1-2 sentence summary). Uses the parse-class model
   for speed/cost.
2. `generate_question` — given a concept and a few past attempts, produce one
   active-recall question. Avoids repeating recent phrasings.
3. `grade_answer` — score an answer 0..1 with short feedback. Threshold at 0.7
   for the boolean `correct` flag the progress view counts.

Mastery is updated by a small EMA in `update_mastery` so a single bad answer
doesn't tank a well-known concept and a single lucky guess doesn't mark
something mastered.
"""
from __future__ import annotations

import json
import logging
import re
import time
from decimal import Decimal
from typing import List, Optional

from anthropic import Anthropic

from app.config import get_settings
from app.exceptions import ClaudeParseError
from app.models.study_attempt import StudyAttempt
from app.models.study_concept import StudyConcept
from app.schemas.study import ExtractedConcept

logger = logging.getLogger(__name__)


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _build_client() -> Anthropic:
    settings = get_settings()
    return Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def _model_text(response: object) -> str:
    """Concatenate all text blocks from an Anthropic response."""
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


def _log_usage(label: str, model: str, response: object, latency_ms: int) -> None:
    try:
        usage = getattr(response, "usage", None)
        in_tokens = getattr(usage, "input_tokens", None)
        out_tokens = getattr(usage, "output_tokens", None)
        logger.info(
            "%s model=%s input_tokens=%s output_tokens=%s latency_ms=%s",
            label,
            model,
            in_tokens,
            out_tokens,
            latency_ms,
        )
    except Exception:
        pass


# ---------- Concept extraction ----------


_EXTRACT_SYSTEM = (
    "You extract atomic study concepts from course material. Return ONLY a "
    "JSON array of {\"title\", \"summary\"} objects — no preamble, no fences. "
    "Title <= 80 chars; summary 1-2 sentences. Skip filler, examples, and "
    "administrivia. One concept per distinct testable idea."
)


def extract_concepts(
    material: str,
    max_concepts: int = 12,
    client: Optional[Anthropic] = None,
) -> List[ExtractedConcept]:
    """Extract atomic concepts from pasted course material.

    Returns at most `max_concepts` concepts, deduped case-insensitively by
    title. Raises `ClaudeParseError` on API or parse failure.
    """
    settings = get_settings()
    client = client or _build_client()

    user_text = (
        f"Extract up to {max_concepts} concepts from the material below. "
        f"Return only the JSON array.\n\n---\n{material}\n---"
    )

    started = time.monotonic()
    try:
        response = client.messages.create(
            model=settings.ANTHROPIC_PARSE_MODEL,
            max_tokens=2048,
            system=_EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": user_text}],
        )
    except Exception as e:
        logger.exception("Anthropic concept-extract call failed")
        raise ClaudeParseError(f"Concept extraction failed: {e}") from e
    _log_usage(
        "Study extract", settings.ANTHROPIC_PARSE_MODEL, response,
        int((time.monotonic() - started) * 1000),
    )

    raw = _strip_fences(_model_text(response))
    if not raw:
        raise ClaudeParseError("Empty response from concept extractor")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("Concept extractor returned non-JSON: %s", raw[:300])
        raise ClaudeParseError(f"Concept extractor returned non-JSON: {e}") from e

    if not isinstance(data, list):
        raise ClaudeParseError("Concept extractor did not return a JSON array")

    seen: set[str] = set()
    out: List[ExtractedConcept] = []
    for item in data[:max_concepts]:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        summary = (item.get("summary") or "").strip()
        if not title or not summary:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(ExtractedConcept(title=title[:256], summary=summary))
    return out


# ---------- Question generation ----------


_QUESTION_SYSTEM = (
    "You write ONE active-recall question for a study concept. Plain text, no "
    "preamble, no answer. Vary phrasing across attempts. Prefer 'why', 'how', "
    "or 'compare' style over yes/no. Keep it under 220 characters."
)


def generate_question(
    concept: StudyConcept,
    recent_attempts: List[StudyAttempt],
    client: Optional[Anthropic] = None,
) -> str:
    """Produce a single question targeting the given concept.

    `recent_attempts` (most recent first) lets the model avoid repeating
    phrasings. We send only the question strings — answers are not relevant
    here and would waste tokens.
    """
    settings = get_settings()
    client = client or _build_client()

    prior = "\n".join(f"- {a.question}" for a in recent_attempts[:5])
    if not prior:
        prior = "(none yet)"

    user_text = (
        f"Concept: {concept.title}\n"
        f"Summary: {concept.summary}\n"
        f"Recent question phrasings to avoid:\n{prior}\n\n"
        f"Write the next question."
    )

    started = time.monotonic()
    try:
        response = client.messages.create(
            model=settings.ANTHROPIC_PARSE_MODEL,
            max_tokens=200,
            system=_QUESTION_SYSTEM,
            messages=[{"role": "user", "content": user_text}],
        )
    except Exception as e:
        logger.exception("Anthropic question-gen call failed")
        raise ClaudeParseError(f"Question generation failed: {e}") from e
    _log_usage(
        "Study question", settings.ANTHROPIC_PARSE_MODEL, response,
        int((time.monotonic() - started) * 1000),
    )

    text = _model_text(response).strip().strip("\"")
    if not text:
        raise ClaudeParseError("Question generator returned empty text")
    return text


# ---------- Grading ----------


_GRADE_SYSTEM = (
    "You grade a student's free-text answer against a concept. Return ONLY a "
    "JSON object: {\"score\": float 0..1, \"feedback\": string}. Score by "
    "correctness AND completeness. Feedback is 1-2 sentences, encouraging, "
    "and points at what was missed (don't dump the full answer if they were "
    "close — nudge them)."
)


def grade_answer(
    concept: StudyConcept,
    question: str,
    answer: str,
    client: Optional[Anthropic] = None,
) -> tuple[float, str]:
    """Grade a free-text answer. Returns (score in 0..1, feedback string)."""
    settings = get_settings()
    client = client or _build_client()

    user_text = (
        f"Concept: {concept.title}\n"
        f"Reference summary: {concept.summary}\n"
        f"Question: {question}\n"
        f"Student answer: {answer}\n\n"
        f"Grade the answer."
    )

    started = time.monotonic()
    try:
        response = client.messages.create(
            model=settings.ANTHROPIC_PARSE_MODEL,
            max_tokens=400,
            system=_GRADE_SYSTEM,
            messages=[{"role": "user", "content": user_text}],
        )
    except Exception as e:
        logger.exception("Anthropic grading call failed")
        raise ClaudeParseError(f"Grading failed: {e}") from e
    _log_usage(
        "Study grade", settings.ANTHROPIC_PARSE_MODEL, response,
        int((time.monotonic() - started) * 1000),
    )

    raw = _strip_fences(_model_text(response))
    if not raw:
        raise ClaudeParseError("Empty response from grader")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("Grader returned non-JSON: %s", raw[:300])
        raise ClaudeParseError(f"Grader returned non-JSON: {e}") from e
    if not isinstance(data, dict):
        raise ClaudeParseError("Grader did not return a JSON object")

    raw_score = data.get("score", 0)
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))
    feedback = str(data.get("feedback") or "").strip() or "No feedback provided."
    return score, feedback


# ---------- Mastery update ----------


# Threshold for marking an attempt as `correct` and counting toward
# times_correct. Kept loose since the grader gives partial credit.
CORRECT_THRESHOLD = 0.7
# EMA weight on the new attempt — small enough that one bad answer doesn't
# crater a well-known concept, large enough that progress shows after a
# couple of attempts.
MASTERY_ALPHA = 0.4


def update_mastery(prior: float, new_score: float) -> float:
    """Exponential moving average of attempt scores, clamped to [0, 1]."""
    blended = (1 - MASTERY_ALPHA) * prior + MASTERY_ALPHA * new_score
    return max(0.0, min(1.0, blended))


def apply_attempt_to_concept(
    concept: StudyConcept, score: float, attempted_at
) -> None:
    """Mutate a StudyConcept in place to reflect a new attempt's score."""
    prior = float(concept.mastery_score or 0)
    concept.mastery_score = Decimal(f"{update_mastery(prior, score):.3f}")
    concept.times_seen = (concept.times_seen or 0) + 1
    if score >= CORRECT_THRESHOLD:
        concept.times_correct = (concept.times_correct or 0) + 1
    concept.last_attempted_at = attempted_at
