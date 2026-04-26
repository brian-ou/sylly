"""CRUD + ownership + validation + cascade tests for grade categories."""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.models.grade_category import GradeCategory
from app.models.syllabus import Syllabus
from app.models.user import User
from app.services.crypto import encrypt_token
from app.services.jwt_tokens import create_access_token


def _auth_headers(user: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest_asyncio.fixture
async def syllabus_for_user(db_session, test_user):
    """Persist a syllabus owned by `test_user` and return it."""
    syl = Syllabus(
        id=uuid.uuid4(),
        user_id=test_user.id,
        filename="econ140.pdf",
        course_name="Macro",
        course_code="ECON 140",
        term="Fall 2026",
        parsed_events={"events": []},
    )
    db_session.add(syl)
    await db_session.commit()
    await db_session.refresh(syl)
    return syl


@pytest_asyncio.fixture
async def other_user(db_session):
    """A second user (used for ownership-404 tests)."""
    u = User(
        id=uuid.uuid4(),
        google_id="google-other-user",
        email="other@example.com",
        name="Other User",
        encrypted_refresh_token=encrypt_token("other-refresh"),
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


@pytest_asyncio.fixture
async def syllabus_for_other(db_session, other_user):
    """Persist a syllabus owned by the *other* user."""
    syl = Syllabus(
        id=uuid.uuid4(),
        user_id=other_user.id,
        filename="other.pdf",
        course_name="Other course",
        course_code="X",
        term="Fall 2026",
        parsed_events={"events": []},
    )
    db_session.add(syl)
    await db_session.commit()
    await db_session.refresh(syl)
    return syl


async def _client(app):
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ---------- create / list ----------


@pytest.mark.asyncio
async def test_create_then_list(app_with_test_db, test_user, syllabus_for_user):
    headers = _auth_headers(test_user)
    async with await _client(app_with_test_db) as client:
        for idx, (name, weight) in enumerate(
            [("Homework", 30), ("Midterm", 25), ("Final", 45)]
        ):
            r = await client.post(
                f"/syllabi/{syllabus_for_user.id}/grade-categories",
                json={"name": name, "weight": weight, "sort_order": idx},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["name"] == name
            assert body["weight"] == weight
            assert body["syllabus_id"] == str(syllabus_for_user.id)

        r = await client.get(
            f"/syllabi/{syllabus_for_user.id}/grade-categories", headers=headers
        )
    assert r.status_code == 200, r.text
    listed = r.json()
    assert [c["name"] for c in listed] == ["Homework", "Midterm", "Final"]


@pytest.mark.asyncio
async def test_create_duplicate_name_case_insensitive_rejected(
    app_with_test_db, test_user, syllabus_for_user
):
    headers = _auth_headers(test_user)
    async with await _client(app_with_test_db) as client:
        r1 = await client.post(
            f"/syllabi/{syllabus_for_user.id}/grade-categories",
            json={"name": "Homework", "weight": 30},
            headers=headers,
        )
        assert r1.status_code == 201
        r2 = await client.post(
            f"/syllabi/{syllabus_for_user.id}/grade-categories",
            json={"name": "homework", "weight": 10},
            headers=headers,
        )
    assert r2.status_code == 400
    assert r2.json()["error"]["code"] == "INVALID_INPUT"


# ---------- validation ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"name": "X", "weight": -1},
        {"name": "X", "weight": 101},
        {"name": "", "weight": 10},
        {"name": "   ", "weight": 10},
        {"name": "X", "weight": 10, "drop_lowest": -1},
    ],
)
async def test_create_validation_errors(
    app_with_test_db, test_user, syllabus_for_user, payload
):
    headers = _auth_headers(test_user)
    async with await _client(app_with_test_db) as client:
        r = await client.post(
            f"/syllabi/{syllabus_for_user.id}/grade-categories",
            json=payload,
            headers=headers,
        )
    assert r.status_code == 422, r.text


# ---------- ownership 404s ----------


@pytest.mark.asyncio
async def test_list_other_users_syllabus_404(
    app_with_test_db, test_user, syllabus_for_other
):
    headers = _auth_headers(test_user)
    async with await _client(app_with_test_db) as client:
        r = await client.get(
            f"/syllabi/{syllabus_for_other.id}/grade-categories", headers=headers
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_create_on_other_users_syllabus_404(
    app_with_test_db, test_user, syllabus_for_other
):
    headers = _auth_headers(test_user)
    async with await _client(app_with_test_db) as client:
        r = await client.post(
            f"/syllabi/{syllabus_for_other.id}/grade-categories",
            json={"name": "Homework", "weight": 30},
            headers=headers,
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_other_users_category_404(
    app_with_test_db,
    db_session,
    test_user,
    other_user,
    syllabus_for_other,
):
    # Seed a category on the OTHER user's syllabus.
    cat = GradeCategory(
        syllabus_id=syllabus_for_other.id,
        name="Homework",
        weight=30,
    )
    db_session.add(cat)
    await db_session.commit()
    await db_session.refresh(cat)

    headers = _auth_headers(test_user)
    async with await _client(app_with_test_db) as client:
        r = await client.patch(
            f"/grade-categories/{cat.id}", json={"weight": 50}, headers=headers
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_other_users_category_404(
    app_with_test_db,
    db_session,
    test_user,
    other_user,
    syllabus_for_other,
):
    cat = GradeCategory(
        syllabus_id=syllabus_for_other.id,
        name="Homework",
        weight=30,
    )
    db_session.add(cat)
    await db_session.commit()
    await db_session.refresh(cat)

    headers = _auth_headers(test_user)
    async with await _client(app_with_test_db) as client:
        r = await client.delete(f"/grade-categories/{cat.id}", headers=headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_unknown_category_404(app_with_test_db, test_user):
    headers = _auth_headers(test_user)
    async with await _client(app_with_test_db) as client:
        r = await client.patch(
            f"/grade-categories/{uuid.uuid4()}",
            json={"weight": 50},
            headers=headers,
        )
    assert r.status_code == 404


# ---------- patch / delete ----------


@pytest.mark.asyncio
async def test_patch_updates_fields(
    app_with_test_db, db_session, test_user, syllabus_for_user
):
    cat = GradeCategory(
        syllabus_id=syllabus_for_user.id, name="Homework", weight=30
    )
    db_session.add(cat)
    await db_session.commit()
    await db_session.refresh(cat)

    headers = _auth_headers(test_user)
    async with await _client(app_with_test_db) as client:
        r = await client.patch(
            f"/grade-categories/{cat.id}",
            json={"weight": 42, "drop_lowest": 2, "notes": "best 5 of 6"},
            headers=headers,
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["weight"] == 42
    assert body["drop_lowest"] == 2
    assert body["notes"] == "best 5 of 6"
    # untouched
    assert body["name"] == "Homework"


@pytest.mark.asyncio
async def test_patch_validation_error(
    app_with_test_db, db_session, test_user, syllabus_for_user
):
    cat = GradeCategory(
        syllabus_id=syllabus_for_user.id, name="Homework", weight=30
    )
    db_session.add(cat)
    await db_session.commit()
    await db_session.refresh(cat)

    headers = _auth_headers(test_user)
    async with await _client(app_with_test_db) as client:
        r = await client.patch(
            f"/grade-categories/{cat.id}", json={"weight": 200}, headers=headers
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_delete_removes_category(
    app_with_test_db, db_session, test_user, syllabus_for_user
):
    cat = GradeCategory(
        syllabus_id=syllabus_for_user.id, name="Homework", weight=30
    )
    db_session.add(cat)
    await db_session.commit()
    await db_session.refresh(cat)
    cat_id = cat.id

    headers = _auth_headers(test_user)
    async with await _client(app_with_test_db) as client:
        r = await client.delete(f"/grade-categories/{cat_id}", headers=headers)
    assert r.status_code == 204

    remaining = (
        await db_session.execute(
            select(GradeCategory).where(GradeCategory.id == cat_id)
        )
    ).scalar_one_or_none()
    assert remaining is None


# ---------- auth ----------


@pytest.mark.asyncio
async def test_endpoints_require_auth(app_with_test_db, syllabus_for_user):
    async with await _client(app_with_test_db) as client:
        r = await client.get(f"/syllabi/{syllabus_for_user.id}/grade-categories")
        assert r.status_code == 401


# ---------- cascade ----------


@pytest.mark.asyncio
async def test_cascade_delete_when_syllabus_deleted(
    app_with_test_db, db_session, test_user, syllabus_for_user
):
    cat = GradeCategory(
        syllabus_id=syllabus_for_user.id, name="Homework", weight=30
    )
    db_session.add(cat)
    await db_session.commit()
    await db_session.refresh(cat)
    cat_id = cat.id

    headers = _auth_headers(test_user)
    async with await _client(app_with_test_db) as client:
        r = await client.delete(
            f"/syllabi/{syllabus_for_user.id}", headers=headers
        )
    assert r.status_code == 204, r.text

    remaining = (
        await db_session.execute(
            select(GradeCategory).where(GradeCategory.id == cat_id)
        )
    ).scalar_one_or_none()
    assert remaining is None
