"""Grade-category routes: list, create, patch, delete.

Powers the FE grade-calculator page. Ownership is enforced via the parent
syllabus, mirroring the pattern in app/routers/events.py and
app/routers/syllabi.py: 404s (not 403s) when the requested resource isn't
owned by the current user.
"""
from __future__ import annotations

import logging
import uuid
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.exceptions import InvalidInputError, NotFoundError
from app.models.grade_category import GradeCategory
from app.models.syllabus import Syllabus
from app.models.user import User
from app.schemas.grade_category import (
    GradeCategoryCreate,
    GradeCategoryRead,
    GradeCategoryUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["grade_categories"])


async def _get_owned_syllabus(
    syllabus_id: uuid.UUID, user: User, db: AsyncSession
) -> Syllabus:
    stmt = select(Syllabus).where(
        Syllabus.id == syllabus_id, Syllabus.user_id == user.id
    )
    syllabus = (await db.execute(stmt)).scalar_one_or_none()
    if syllabus is None:
        raise NotFoundError("Syllabus not found")
    return syllabus


async def _get_owned_category(
    category_id: uuid.UUID, user: User, db: AsyncSession
) -> GradeCategory:
    """Load a grade category, 404 if it doesn't exist or isn't owned."""
    stmt = (
        select(GradeCategory)
        .join(Syllabus, GradeCategory.syllabus_id == Syllabus.id)
        .where(GradeCategory.id == category_id, Syllabus.user_id == user.id)
    )
    category = (await db.execute(stmt)).scalar_one_or_none()
    if category is None:
        raise NotFoundError("Grade category not found")
    return category


@router.get(
    "/syllabi/{syllabus_id}/grade-categories",
    response_model=List[GradeCategoryRead],
    summary="List grade categories for a syllabus",
)
async def list_grade_categories(
    syllabus_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[GradeCategoryRead]:
    await _get_owned_syllabus(syllabus_id, current_user, db)
    stmt = (
        select(GradeCategory)
        .where(GradeCategory.syllabus_id == syllabus_id)
        .order_by(GradeCategory.sort_order, GradeCategory.name)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [GradeCategoryRead.model_validate(r) for r in rows]


@router.post(
    "/syllabi/{syllabus_id}/grade-categories",
    response_model=GradeCategoryRead,
    status_code=201,
    summary="Add a grade category to a syllabus",
)
async def create_grade_category(
    syllabus_id: uuid.UUID,
    body: GradeCategoryCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GradeCategoryRead:
    await _get_owned_syllabus(syllabus_id, current_user, db)
    category = GradeCategory(
        syllabus_id=syllabus_id,
        name=body.name,
        weight=body.weight,
        drop_lowest=body.drop_lowest,
        notes=body.notes,
        sort_order=body.sort_order,
    )
    db.add(category)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise InvalidInputError(
            f"A grade category named {body.name!r} already exists for this syllabus"
        ) from e
    await db.refresh(category)
    return GradeCategoryRead.model_validate(category)


@router.patch(
    "/grade-categories/{category_id}",
    response_model=GradeCategoryRead,
    summary="Update fields on a grade category",
)
async def update_grade_category(
    category_id: uuid.UUID,
    body: GradeCategoryUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GradeCategoryRead:
    category = await _get_owned_category(category_id, current_user, db)
    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(category, field, value)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise InvalidInputError(
            "A grade category with that name already exists for this syllabus"
        ) from e
    await db.refresh(category)
    return GradeCategoryRead.model_validate(category)


@router.delete(
    "/grade-categories/{category_id}",
    status_code=204,
    summary="Delete a grade category",
)
async def delete_grade_category(
    category_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    category = await _get_owned_category(category_id, current_user, db)
    await db.delete(category)
    await db.commit()
