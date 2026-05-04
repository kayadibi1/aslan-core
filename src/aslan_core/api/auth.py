"""JWT token utilities and password hashing for the Aslan API."""

from __future__ import annotations

import hashlib
import importlib
import os
import sys
import warnings
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from jose import JWTError, jwt

# ---------------------------------------------------------------------------
# passlib + bcrypt >= 4.1 compatibility shim
#
# passlib 1.7.x triggers two issues on modern Python / bcrypt stacks:
#   1. It imports the deprecated stdlib ``crypt`` module (removed in 3.13).
#   2. Its internal 256-byte wrap-bug probe crashes on bcrypt >= 4.1 which
#      refuses passwords > 72 bytes.
#
# We patch ``bcrypt.hashpw`` / ``bcrypt.checkpw`` to silently truncate
# the password to 72 bytes (which is bcrypt's native limit anyway) so
# passlib's wrap-bug detection probe no longer explodes.
# See https://foss.heptapod.net/python-libs/passlib/-/issues/190
# ---------------------------------------------------------------------------


def _patch_bcrypt_truncation() -> None:
    """Monkey-patch bcrypt to truncate passwords to 72 bytes."""
    _bcrypt_lib: Any = importlib.import_module("bcrypt")

    orig_hashpw = _bcrypt_lib.hashpw
    orig_checkpw = _bcrypt_lib.checkpw

    def patched_hashpw(password: bytes, salt: bytes) -> bytes:
        return orig_hashpw(password[:72], salt)  # type: ignore[no-any-return]

    def patched_checkpw(password: bytes, hashed_password: bytes) -> bool:
        return orig_checkpw(password[:72], hashed_password)  # type: ignore[no-any-return]

    _bcrypt_lib.hashpw = patched_hashpw
    _bcrypt_lib.checkpw = patched_checkpw


if "bcrypt" not in sys.modules:
    # Import bcrypt first so it's in sys.modules before passlib touches it.
    importlib.import_module("bcrypt")

_patch_bcrypt_truncation()

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=".*'crypt' is deprecated.*",
        category=DeprecationWarning,
    )
    from passlib.context import CryptContext

JWT_ALGORITHM = "HS256"
ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=7)
ISSUER = "aslan-core"
AUDIENCE = "aslan-api"

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def get_jwt_secret() -> str:
    """Read the JWT signing secret from the environment.

    Raises ``RuntimeError`` when the secret is missing or shorter than
    32 characters.
    """
    secret = os.environ.get("ASLAN_JWT_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("ASLAN_JWT_SECRET must be >= 32 characters")
    return secret


def create_access_token(user_id: UUID, role: str, secret: str) -> str:
    """Create a signed JWT access token for *user_id* with *role*."""
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(user_id),
        "exp": now + ACCESS_TTL,
        "nbf": now,
        "iat": now,
        "jti": str(uuid4()),
        "typ": "access",
        "role": role,
    }
    token: str = jwt.encode(claims, secret, algorithm=JWT_ALGORITHM)
    return token


def verify_access_token(token: str, secret: str) -> dict[str, object]:
    """Verify and decode an access token. Raises ``JWTError`` on failure."""
    payload: dict[str, object] = jwt.decode(
        token,
        secret,
        algorithms=[JWT_ALGORITHM],
        issuer=ISSUER,
        audience=AUDIENCE,
    )
    if payload.get("typ") != "access":
        raise JWTError("Token type must be 'access'")
    required = {"iss", "aud", "sub", "exp", "nbf", "iat", "jti", "typ"}
    missing = required - set(payload.keys())
    if missing:
        raise JWTError(f"Missing claims: {missing}")
    return payload


def create_refresh_token() -> tuple[str, str]:
    """Create a refresh token. Returns ``(raw_token, sha256_hash)``."""
    raw = str(uuid4()) + str(uuid4())
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    return raw, hashed


def hash_password(password: str) -> str:
    """Hash *password* using bcrypt via passlib."""
    result: str = _pwd_ctx.hash(password)
    return result


def verify_password(plain: str, hashed: str) -> bool:
    """Return ``True`` when *plain* matches the bcrypt *hashed* value."""
    result: bool = _pwd_ctx.verify(plain, hashed)
    return result
