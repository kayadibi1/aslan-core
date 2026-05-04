"""FastAPI dependency injection helpers.

Provides:
- ``get_session``: yields an async SQLAlchemy session from the app-level
  engine created during the lifespan startup.
- ``get_current_user``: decodes the Bearer JWT, verifies claims, checks
  user existence and active status, returns a :class:`Principal`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.api.auth import get_jwt_secret, verify_access_token
from aslan_core.db.session import create_session_factory
from aslan_core.query.schemas import Principal

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Yield an async session from the engine stored in ``app.state``."""
    engine = request.app.state.engine
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """Decode JWT, verify claims, check user is active."""
    try:
        secret = get_jwt_secret()
        payload = verify_access_token(token, secret)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid credentials") from None

    try:
        user_id = UUID(str(payload["sub"]))
    except (ValueError, KeyError):
        raise HTTPException(status_code=401, detail="Invalid credentials") from None

    row = await session.execute(
        text("SELECT role, is_active FROM auth.user WHERE user_id = :uid"),
        {"uid": user_id},
    )
    user = row.first()
    if not user or not user[1]:  # not found or not active
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return Principal(user_id=user_id, role=user[0], is_active=True)


__all__ = [
    "get_current_user",
    "get_session",
    "oauth2_scheme",
]
