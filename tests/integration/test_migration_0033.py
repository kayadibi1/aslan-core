"""Migration 0033 — auth schema with user + refresh_token tables."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_USER_EMAIL = "test0033@example.com"
_USER_EMAIL_2 = "test0033b@example.com"


async def _wipe_test_data(session: AsyncSession) -> None:
    await session.execute(
        text(
            "DELETE FROM auth.refresh_token USING auth.user u "
            "WHERE auth.refresh_token.user_id = u.user_id "
            "  AND u.email IN (:e1, :e2)"
        ),
        {"e1": _USER_EMAIL, "e2": _USER_EMAIL_2},
    )
    await session.execute(
        text("DELETE FROM auth.user WHERE email IN (:e1, :e2)"),
        {"e1": _USER_EMAIL, "e2": _USER_EMAIL_2},
    )
    await session.commit()


# -- Schema existence ---------------------------------------------------------


async def test_auth_schema_exists(session: AsyncSession) -> None:
    result = await session.execute(
        text("SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'auth'")
    )
    assert result.scalar() == "auth"


# -- Table existence ----------------------------------------------------------


async def test_user_table_exists(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'auth' AND table_name = 'user' "
            "ORDER BY ordinal_position"
        )
    )
    columns = [r[0] for r in result.fetchall()]
    assert "user_id" in columns
    assert "email" in columns
    assert "password_hash" in columns
    assert "role" in columns
    assert "is_active" in columns
    assert "created_at" in columns


async def test_refresh_token_table_exists(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'auth' AND table_name = 'refresh_token' "
            "ORDER BY ordinal_position"
        )
    )
    columns = [r[0] for r in result.fetchall()]
    assert "token_id" in columns
    assert "user_id" in columns
    assert "token_family" in columns
    assert "token_hash" in columns
    assert "expires_at" in columns
    assert "revoked" in columns
    assert "created_at" in columns


# -- CHECK constraint: role ---------------------------------------------------


async def test_role_check_rejects_invalid(session: AsyncSession) -> None:
    """role CHECK must reject values outside ('user', 'admin')."""
    with pytest.raises(Exception, match=r"check|violates"):
        await session.execute(
            text(
                "INSERT INTO auth.user (email, password_hash, role) "
                "VALUES (:email, 'hash', 'superuser')"
            ),
            {"email": _USER_EMAIL},
        )
        await session.commit()
    await session.rollback()


async def test_role_check_accepts_user(session: AsyncSession) -> None:
    """role CHECK must accept 'user'."""
    await session.execute(
        text("INSERT INTO auth.user (email, password_hash, role) VALUES (:email, 'hash', 'user')"),
        {"email": _USER_EMAIL},
    )
    await session.commit()
    await _wipe_test_data(session)


async def test_role_check_accepts_admin(session: AsyncSession) -> None:
    """role CHECK must accept 'admin'."""
    await session.execute(
        text("INSERT INTO auth.user (email, password_hash, role) VALUES (:email, 'hash', 'admin')"),
        {"email": _USER_EMAIL},
    )
    await session.commit()
    await _wipe_test_data(session)


# -- UNIQUE constraint: email -------------------------------------------------


async def test_email_unique_constraint(session: AsyncSession) -> None:
    """email UNIQUE must reject a duplicate."""
    await session.execute(
        text("INSERT INTO auth.user (email, password_hash) VALUES (:email, 'hash1')"),
        {"email": _USER_EMAIL},
    )
    await session.commit()

    with pytest.raises(Exception, match=r"unique|duplicate"):
        await session.execute(
            text("INSERT INTO auth.user (email, password_hash) VALUES (:email, 'hash2')"),
            {"email": _USER_EMAIL},
        )
        await session.commit()
    await session.rollback()
    await _wipe_test_data(session)


# -- Indexes exist ------------------------------------------------------------


async def test_rt_user_id_index_exists(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'auth' "
            "  AND tablename = 'refresh_token' "
            "  AND indexname = 'rt_user_id'"
        )
    )
    assert result.scalar() == "rt_user_id"


async def test_rt_family_index_exists(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'auth' "
            "  AND tablename = 'refresh_token' "
            "  AND indexname = 'rt_family'"
        )
    )
    assert result.scalar() == "rt_family"


# -- FK cascade: delete user removes tokens -----------------------------------


async def test_refresh_token_cascade_delete(session: AsyncSession) -> None:
    """Deleting a user must cascade-delete their refresh tokens."""
    await session.execute(
        text("INSERT INTO auth.user (email, password_hash) VALUES (:email, 'hash')"),
        {"email": _USER_EMAIL},
    )
    await session.commit()

    result = await session.execute(
        text("SELECT user_id FROM auth.user WHERE email = :email"),
        {"email": _USER_EMAIL},
    )
    user_id = result.scalar()

    family = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO auth.refresh_token "
            "(user_id, token_family, token_hash, expires_at) "
            "VALUES (:uid, CAST(:fam AS uuid), :thash, now() + interval '7 days')"
        ),
        {"uid": user_id, "fam": family, "thash": "a" * 64},
    )
    await session.commit()

    await session.execute(
        text("DELETE FROM auth.user WHERE user_id = :uid"),
        {"uid": user_id},
    )
    await session.commit()

    result = await session.execute(
        text("SELECT COUNT(*) FROM auth.refresh_token WHERE user_id = :uid"),
        {"uid": user_id},
    )
    assert result.scalar() == 0
