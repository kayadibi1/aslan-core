"""Bitemporal Research API — API-key auth + feature flag gating.

Per ``docs/specs/bitemporal-research-api/SCOPE.md`` D5, D6, D27.

The master flag ``BITEMPORAL_API_ENABLED`` gates every research
endpoint. When ``false``, all endpoints (except the unauthenticated
public verification surfaces ``/verify/moat-2``, ``/healthz``,
``/version``) return ``503 FEATURE_DISABLED``.

API keys are the only auth scheme in v1. The ``X-Aslan-Api-Key``
header carries the secret; the server hashes (argon2id) and looks up
``aslan_core.api_key`` for ``rate_tier``, ``pii_unredacted``, and
expiry / revocation state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.api.deps import get_session

# Argon2id is the secret-hash KDF for API keys per SCOPE.md D5.
# Defaults are reasonable for v1 (memory_cost=64MiB, time_cost=3,
# parallelism=4); tune via environment if a different cost is needed.
_PASSWORD_HASHER = PasswordHasher()


def hash_secret(secret: str) -> str:
    """Hash an API-key secret with argon2id.

    Used by the key-issuance CLI (not exposed via the API).
    """
    return _PASSWORD_HASHER.hash(secret)


def verify_secret(stored_hash: str, presented_secret: str) -> bool:
    """Constant-time argon2id verify with backward-compat fallback.

    Returns True if `presented_secret` argon2-verifies against
    `stored_hash`. Falls back to plaintext equality only when the
    stored hash does NOT look like an argon2 string (legacy seed
    keys); legacy verification is documented for one release and
    removed in v1.0.0.
    """
    if not stored_hash:
        return False
    if stored_hash.startswith("$argon2"):
        try:
            return _PASSWORD_HASHER.verify(stored_hash, presented_secret)
        except VerifyMismatchError:
            return False
    # Backward-compat for seed/test keys with non-argon2 hashes.
    # Constant-time compare via hmac.compare_digest.
    import hmac

    return hmac.compare_digest(stored_hash, presented_secret)


@dataclass(frozen=True)
class ApiKeyPrincipal:
    """Identity established by the X-Aslan-Api-Key header."""

    key_id: UUID
    rate_tier: str  # internal | partner | public
    pii_unredacted: bool
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class FeatureFlagState:
    """Snapshot of the feature flags active for this request."""

    enabled: dict[str, bool]
    text_values: dict[str, str]


_PUBLIC_PATHS = ("/verify/moat-2", "/healthz", "/version")


def _is_public(path: str) -> bool:
    return any(path.endswith(p) for p in _PUBLIC_PATHS)


async def _load_feature_flags(session: AsyncSession) -> FeatureFlagState:
    """Load the global flag state from aslan_core.feature_flags.

    Returns a snapshot suitable for inclusion in the response envelope.
    """
    enabled: dict[str, bool] = {}
    text_values: dict[str, str] = {}

    rows = await session.execute(
        text(
            "SELECT flag_name, value_bool, value_text "
            "FROM aslan_core.feature_flags WHERE scope = 'global'"
        )
    )
    for name, value_bool, value_text in rows:
        if value_bool is not None:
            enabled[name] = bool(value_bool)
        if value_text is not None:
            text_values[name] = str(value_text)

    return FeatureFlagState(enabled=enabled, text_values=text_values)


async def get_feature_flags(
    session: AsyncSession = Depends(get_session),
) -> FeatureFlagState:
    return await _load_feature_flags(session)


async def require_master_flag(
    request: Request,
    flags: FeatureFlagState = Depends(get_feature_flags),
) -> FeatureFlagState:
    """Reject the request if BITEMPORAL_API_ENABLED is false.

    Public endpoints (verify/moat-2, healthz, version) bypass this.
    """
    if _is_public(request.url.path):
        return flags
    if not flags.enabled.get("BITEMPORAL_API_ENABLED", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "type": "https://docs.aslanterminal.com/errors/FEATURE_DISABLED",
                "title": "Bitemporal Research API is disabled",
                "code": "FEATURE_DISABLED",
                "detail": "BITEMPORAL_API_ENABLED is false in this environment.",
            },
        )
    return flags


async def get_principal(
    request: Request,
    x_aslan_api_key: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyPrincipal | None:
    """Resolve the API key principal.

    Returns ``None`` for public endpoints; raises 401 for missing/invalid
    keys on authenticated endpoints. PROVISIONAL: secret hashing via
    argon2id is the SCOPE.md D5 design, but in v1 the lookup just
    matches the secret_hash column directly with the supplied key_id +
    secret pair. Final argon2id integration lands with the auth
    refactor in v1.1.
    """
    if _is_public(request.url.path):
        return None

    if not x_aslan_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "https://docs.aslanterminal.com/errors/AUTH_INVALID",
                "title": "Missing X-Aslan-Api-Key header",
                "code": "AUTH_INVALID",
            },
        )

    # v1 placeholder: split the header on a colon → (key_id, secret).
    # In v1.1 we rotate to a single-token format with KDF lookup.
    if ":" not in x_aslan_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "https://docs.aslanterminal.com/errors/AUTH_INVALID",
                "title": "Malformed X-Aslan-Api-Key (expected key_id:secret)",
                "code": "AUTH_INVALID",
            },
        )
    raw_key_id, raw_secret = x_aslan_api_key.split(":", 1)
    try:
        key_id = UUID(raw_key_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "https://docs.aslanterminal.com/errors/AUTH_INVALID",
                "title": "X-Aslan-Api-Key key_id is not a UUID",
                "code": "AUTH_INVALID",
            },
        ) from exc

    row = (
        await session.execute(
            text(
                "SELECT key_id, secret_hash, rate_tier, pii_unredacted, scopes, "
                "       expires_at, revoked_at "
                "FROM aslan_core.api_key WHERE key_id = :key_id"
            ),
            {"key_id": str(key_id)},
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_INVALID", "title": "Unknown API key"},
        )

    # Argon2id verification (with backward-compat for seed/test keys).
    if not verify_secret(row.secret_hash, raw_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_INVALID", "title": "Invalid API key secret"},
        )

    if row.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_INVALID", "title": "API key revoked"},
        )
    if row.expires_at is not None and row.expires_at < datetime.now(tz=UTC):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_INVALID", "title": "API key expired"},
        )

    # Side-effect: best-effort last_used_at touch (does not block on failure).
    try:
        await session.execute(
            text(
                "UPDATE aslan_core.api_key SET last_used_at = now() "
                "WHERE key_id = :key_id"
            ),
            {"key_id": str(key_id)},
        )
        await session.commit()
    except Exception:  # noqa: S110 — best-effort last_used touch
        pass

    return ApiKeyPrincipal(
        key_id=row.key_id,
        rate_tier=row.rate_tier,
        pii_unredacted=bool(row.pii_unredacted),
        scopes=tuple(row.scopes or ()),
    )


def _equals_legacy_hash(stored: str, presented: str) -> bool:
    """Backward-compat for any test keys seeded as plain hex.

    Returns False for empty stored. Keep simple; no constant-time
    compare needed in v1 (placeholder path).
    """
    return bool(stored) and stored == presented


__all__ = [
    "ApiKeyPrincipal",
    "FeatureFlagState",
    "get_feature_flags",
    "get_principal",
    "require_master_flag",
]
