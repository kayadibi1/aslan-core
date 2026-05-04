"""Tests for aslan_core.api.auth — JWT utilities and password hashing."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

jose = pytest.importorskip("jose")
from jose import JWTError, jwt  # noqa: E402

from aslan_core.api.auth import (  # noqa: E402
    AUDIENCE,
    ISSUER,
    JWT_ALGORITHM,
    create_access_token,
    create_refresh_token,
    get_jwt_secret,
    hash_password,
    verify_access_token,
    verify_password,
)

_SECRET = "a" * 64  # deterministic 64-char secret for tests


@pytest.fixture()
def jwt_secret_env(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set ASLAN_JWT_SECRET in the environment and return the value."""
    monkeypatch.setenv("ASLAN_JWT_SECRET", _SECRET)
    return _SECRET


# --- get_jwt_secret ----------------------------------------------------------


def test_get_jwt_secret_raises_on_short_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASLAN_JWT_SECRET", "short")
    with pytest.raises(RuntimeError, match="must be >= 32 characters"):
        get_jwt_secret()


def test_get_jwt_secret_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASLAN_JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="must be >= 32 characters"):
        get_jwt_secret()


def test_get_jwt_secret_returns_valid_secret(jwt_secret_env: str) -> None:
    assert get_jwt_secret() == jwt_secret_env


# --- create_access_token -----------------------------------------------------


def test_create_access_token_contains_all_required_claims() -> None:
    user_id = uuid4()
    token = create_access_token(user_id, "admin", _SECRET)
    payload = jwt.decode(token, _SECRET, algorithms=[JWT_ALGORITHM], audience=AUDIENCE)
    required = {"iss", "aud", "sub", "exp", "nbf", "iat", "jti", "typ", "role"}
    assert required <= set(payload.keys())
    assert payload["iss"] == ISSUER
    assert payload["aud"] == AUDIENCE
    assert payload["sub"] == str(user_id)
    assert payload["typ"] == "access"
    assert payload["role"] == "admin"


def test_create_access_token_sub_is_string_uuid() -> None:
    user_id = uuid4()
    token = create_access_token(user_id, "viewer", _SECRET)
    payload = jwt.decode(token, _SECRET, algorithms=[JWT_ALGORITHM], audience=AUDIENCE)
    # sub must be a valid UUID string
    UUID(payload["sub"])


def test_create_access_token_jti_is_unique() -> None:
    uid = uuid4()
    t1 = create_access_token(uid, "admin", _SECRET)
    t2 = create_access_token(uid, "admin", _SECRET)
    p1 = jwt.decode(t1, _SECRET, algorithms=[JWT_ALGORITHM], audience=AUDIENCE)
    p2 = jwt.decode(t2, _SECRET, algorithms=[JWT_ALGORITHM], audience=AUDIENCE)
    assert p1["jti"] != p2["jti"]


# --- verify_access_token -----------------------------------------------------


def test_verify_access_token_succeeds_with_valid_token() -> None:
    user_id = uuid4()
    token = create_access_token(user_id, "admin", _SECRET)
    payload = verify_access_token(token, _SECRET)
    assert payload["sub"] == str(user_id)
    assert payload["role"] == "admin"


def test_verify_access_token_rejects_expired_token() -> None:
    """Manually craft an already-expired token."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(uuid4()),
        "exp": now - timedelta(seconds=10),
        "nbf": now - timedelta(minutes=20),
        "iat": now - timedelta(minutes=20),
        "jti": str(uuid4()),
        "typ": "access",
        "role": "admin",
    }
    token = jwt.encode(claims, _SECRET, algorithm=JWT_ALGORITHM)
    with pytest.raises(JWTError):
        verify_access_token(token, _SECRET)


def test_verify_access_token_rejects_wrong_issuer() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    claims = {
        "iss": "evil-issuer",
        "aud": AUDIENCE,
        "sub": str(uuid4()),
        "exp": now + timedelta(minutes=15),
        "nbf": now,
        "iat": now,
        "jti": str(uuid4()),
        "typ": "access",
        "role": "admin",
    }
    token = jwt.encode(claims, _SECRET, algorithm=JWT_ALGORITHM)
    with pytest.raises(JWTError):
        verify_access_token(token, _SECRET)


def test_verify_access_token_rejects_wrong_audience() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER,
        "aud": "wrong-audience",
        "sub": str(uuid4()),
        "exp": now + timedelta(minutes=15),
        "nbf": now,
        "iat": now,
        "jti": str(uuid4()),
        "typ": "access",
        "role": "admin",
    }
    token = jwt.encode(claims, _SECRET, algorithm=JWT_ALGORITHM)
    with pytest.raises(JWTError):
        verify_access_token(token, _SECRET)


def test_verify_access_token_rejects_wrong_typ() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(uuid4()),
        "exp": now + timedelta(minutes=15),
        "nbf": now,
        "iat": now,
        "jti": str(uuid4()),
        "typ": "refresh",
        "role": "admin",
    }
    token = jwt.encode(claims, _SECRET, algorithm=JWT_ALGORITHM)
    with pytest.raises(JWTError, match="Token type must be 'access'"):
        verify_access_token(token, _SECRET)


def test_verify_access_token_rejects_missing_claims() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    # omit jti and nbf
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(uuid4()),
        "exp": now + timedelta(minutes=15),
        "iat": now,
        "typ": "access",
        "role": "admin",
    }
    token = jwt.encode(claims, _SECRET, algorithm=JWT_ALGORITHM)
    with pytest.raises(JWTError, match="Missing claims"):
        verify_access_token(token, _SECRET)


def test_verify_access_token_rejects_wrong_secret() -> None:
    token = create_access_token(uuid4(), "admin", _SECRET)
    with pytest.raises(JWTError):
        verify_access_token(token, "b" * 64)


# --- password hashing --------------------------------------------------------


def test_hash_password_and_verify_round_trip() -> None:
    pw = "hunter2-strong-pass!"
    h = hash_password(pw)
    assert verify_password(pw, h)


def test_verify_password_rejects_wrong_password() -> None:
    h = hash_password("correct-password")
    assert not verify_password("wrong-password", h)


def test_hash_password_produces_different_hashes() -> None:
    """bcrypt should salt; same input produces different hashes."""
    h1 = hash_password("same")
    h2 = hash_password("same")
    assert h1 != h2


# --- refresh tokens ----------------------------------------------------------


def test_create_refresh_token_returns_64_char_hex_hash() -> None:
    _raw, hashed = create_refresh_token()
    assert len(hashed) == 64
    # must be valid hex
    int(hashed, 16)


def test_create_refresh_token_raw_is_nonempty() -> None:
    raw, _ = create_refresh_token()
    assert len(raw) > 0


def test_create_refresh_token_hash_matches_raw() -> None:
    import hashlib

    raw, hashed = create_refresh_token()
    assert hashlib.sha256(raw.encode()).hexdigest() == hashed
