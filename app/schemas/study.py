"""Study-tool Pydantic schemas: concepts, attempts, quiz cycle, progress."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


# ---------- Material ingestion ----------


class StudyMaterialIngestRequest(BaseModel):
    """Pasted course material to extract concepts from."""

    material: str = Field(..., min_length=20)
    syllabus_id: Optional[uuid.UUID] = None
    # Cap how many concepts the model is allowed to emit. Helps keep output
    # tokens bounded on long pastes.
    max_concepts: int = Field(default=12, ge=1, le=40)


class ExtractedConcept(BaseModel):
    """One concept as returned by the extractor model."""

    title: str
    summary: str


class StudyConceptRead(BaseModel):
    id: uuid.UUID
    syllabus_id: Optional[uuid.UUID] = None
    title: str
    summary: str
    mastery_score: float
    times_seen: int
    times_correct: int
    last_attempted_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class StudyMaterialIngestResponse(BaseModel):
    syllabus_id: Optional[uuid.UUID] = None
    concepts: List[StudyConceptRead]


# ---------- Quiz cycle ----------


class QuizNextRequest(BaseModel):
    """Ask for the next quiz question.

    `syllabus_id` narrows to a single course; otherwise the picker considers
    all the user's concepts. `concept_id` lets the caller pin a specific
    concept (e.g. when the chat agent says "let's try that one again").
    """

    syllabus_id: Optional[uuid.UUID] = None
    concept_id: Optional[uuid.UUID] = None


class QuizQuestion(BaseModel):
    concept_id: uuid.UUID
    concept_title: str
    question: str


class QuizGradeRequest(BaseModel):
    concept_id: uuid.UUID
    question: str
    answer: str = Field(..., min_length=1)


class QuizGradeResponse(BaseModel):
    correct: bool
    score: float
    feedback: str
    concept: StudyConceptRead


# ---------- Progress ----------


class StudyProgressBucket(BaseModel):
    """Summary stats for one syllabus (or the unscoped bucket)."""

    syllabus_id: Optional[uuid.UUID] = None
    course_label: Optional[str] = None
    concept_count: int
    mastered_count: int  # mastery_score >= 0.8
    struggling_count: int  # times_seen >= 2 and mastery_score < 0.4
    total_attempts: int
    average_mastery: float
    next_exam_at: Optional[datetime] = None


class StudyProgressResponse(BaseModel):
    buckets: List[StudyProgressBucket]
