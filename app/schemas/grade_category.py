"""GradeCategory-related Pydantic schemas."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class GradeCategoryBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    weight: float = Field(..., ge=0, le=100)
    drop_lowest: int = Field(default=0, ge=0)
    notes: Optional[str] = None
    sort_order: int = 0

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class GradeCategoryCreate(GradeCategoryBase):
    pass


class GradeCategoryUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    weight: Optional[float] = Field(default=None, ge=0, le=100)
    drop_lowest: Optional[int] = Field(default=None, ge=0)
    notes: Optional[str] = None
    sort_order: Optional[int] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class GradeCategoryRead(GradeCategoryBase):
    id: uuid.UUID
    syllabus_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
