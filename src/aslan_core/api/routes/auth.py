"""Auth routes: register, login, refresh.

All auth errors return a uniform ``{"detail": "Invalid credentials",
"status_code": 401}`` to avoid leaking whether an email exists.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.api.auth import (
    REFRESH_TTL,
    create_access_token,
    create_refresh_token,
    get_jwt_secret,
    hash_password,
    verify_password,
)
from aslan_core.api.deps import get_session

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=10)
    invite_code: str


class RegisterResponse(BaseModel):
    user_id: UUID
    email: str


class LoginRequest(BaseModel):
    email: str
    password: str


_BEARER: str = "bearer"


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = _BEARER


class RefreshRequest(BaseModel):
    refresh_token: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/register", response_model=RegisterResponse)
async def register(
    body: RegisterRequest,
    session: AsyncSession = Depends(get_session),
) -> RegisterResponse:
    """Register a new user.  Requires a valid invite code."""
    expected_code = os.environ.get("ASLAN_INVITE_CODE", "")
    if not expected_code or body.invite_code != expected_code:
        raise HTTPException(status_code=403, detail="Invalid invite code")

    # Check duplicate email
    existing = await session.execute(
        text("SELECT 1 FROM auth.user WHERE email = :email"),
        {"email": body.email},
    )
    if existing.first() is not None:
        raise HTTPException(status_code=400, detail="Email already registered")

    pw_hash = hash_password(body.password)
    user_id = uuid4()

    await session.execute(
        text(
            "INSERT INTO auth.user (user_id, email, password_hash) VALUES (:uid, :email, :pw_hash)"
        ),
        {"uid": user_id, "email": body.email, "pw_hash": pw_hash},
    )
    await session.commit()

    return RegisterResponse(user_id=user_id, email=body.email)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """Authenticate with email + password. Returns access + refresh tokens."""
    row = await session.execute(
        text("SELECT user_id, password_hash, role, is_active FROM auth.user WHERE email = :email"),
        {"email": body.email},
    )
    user = row.first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not user[3]:  # is_active
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not verify_password(body.password, user[1]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user_id: UUID = user[0]
    role: str = user[2]
    secret = get_jwt_secret()
    access_token = create_access_token(user_id, role, secret)

    raw_refresh, refresh_hash = create_refresh_token()
    family = uuid4()
    expires_at = datetime.now(UTC) + REFRESH_TTL

    await session.execute(
        text(
            "INSERT INTO auth.refresh_token "
            "(user_id, token_family, token_hash, expires_at) "
            "VALUES (:uid, :family, :hash, :exp)"
        ),
        {
            "uid": user_id,
            "family": family,
            "hash": refresh_hash,
            "exp": expires_at,
        },
    )
    await session.commit()

    return TokenResponse(access_token=access_token, refresh_token=raw_refresh)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """Rotate refresh token per spec section 4.6.

    Hash the presented token, look it up, revoke it, issue a new pair
    in the same family. If a revoked token is re-presented, the entire
    family is revoked (theft detection).
    """
    presented_hash = hashlib.sha256(body.refresh_token.encode()).hexdigest()

    # Look up the token row
    row = await session.execute(
        text(
            "SELECT token_id, user_id, token_family, revoked, expires_at "
            "FROM auth.refresh_token "
            "WHERE token_hash = :hash"
        ),
        {"hash": presented_hash},
    )
    token_row = row.first()

    if not token_row:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token_id: UUID = token_row[0]
    user_id: UUID = token_row[1]
    family: UUID = token_row[2]
    revoked: bool = token_row[3]
    expires_at: datetime = token_row[4]

    if revoked:
        # Reuse detected — revoke entire family
        await session.execute(
            text("UPDATE auth.refresh_token SET revoked = TRUE WHERE token_family = :family"),
            {"family": family},
        )
        await session.commit()
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if expires_at < datetime.now(UTC):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Revoke the current token
    await session.execute(
        text("UPDATE auth.refresh_token SET revoked = TRUE WHERE token_id = :tid"),
        {"tid": token_id},
    )

    # Check user still active
    user_row = await session.execute(
        text("SELECT role, is_active FROM auth.user WHERE user_id = :uid"),
        {"uid": user_id},
    )
    user = user_row.first()
    if not user or not user[1]:
        await session.commit()
        raise HTTPException(status_code=401, detail="Invalid credentials")

    role: str = user[0]
    secret = get_jwt_secret()
    access_token = create_access_token(user_id, role, secret)

    raw_refresh, refresh_hash = create_refresh_token()
    new_expires = datetime.now(UTC) + REFRESH_TTL

    await session.execute(
        text(
            "INSERT INTO auth.refresh_token "
            "(user_id, token_family, token_hash, expires_at) "
            "VALUES (:uid, :family, :hash, :exp)"
        ),
        {
            "uid": user_id,
            "family": family,
            "hash": refresh_hash,
            "exp": new_expires,
        },
    )
    await session.commit()

    return TokenResponse(access_token=access_token, refresh_token=raw_refresh)


__all__ = ["router"]
