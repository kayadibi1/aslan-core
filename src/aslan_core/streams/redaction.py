"""GDPR Art. 17 helpers for stream redaction enforcement."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from redis.asyncio import Redis


REDACTION_CACHE_TTL_SECONDS = 24 * 3600


@dataclass(frozen=True, slots=True)
class RedactionRegistryEntry:
    event_id: UUID
    redaction_reason: str
    original_stream: str
    redacted_payload: dict[str, Any]
    redacted_payload_hash: str
    original_payload_hash: str


def redaction_lock_key(event_id: UUID) -> str:
    """String fed to ``hashtextextended`` by readers and the DB function."""
    return f"streams.redaction:{event_id}"


def redaction_cache_key(event_id: UUID) -> str:
    return f"streams:redaction_registry:{event_id}"


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    """Stable SHA-256 over JSON-compatible event payload dictionaries."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


async def acquire_event_lock(session: AsyncSession, event_id: UUID) -> None:
    """Acquire the per-event transaction-scoped redaction advisory lock."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": redaction_lock_key(event_id)},
    )


async def fetch_registry_entry(
    session: AsyncSession,
    event_id: UUID,
) -> RedactionRegistryEntry | None:
    row = (
        await session.execute(
            text(
                """
                SELECT event_id, redaction_reason, original_stream,
                       redacted_payload, redacted_payload_hash,
                       original_payload_hash
                FROM streams.redaction_registry
                WHERE event_id = :event_id
                """
            ),
            {"event_id": str(event_id)},
        )
    ).one_or_none()
    if row is None:
        return None
    return RedactionRegistryEntry(
        event_id=row.event_id,
        redaction_reason=str(row.redaction_reason),
        original_stream=str(row.original_stream),
        redacted_payload=dict(row.redacted_payload),
        redacted_payload_hash=str(row.redacted_payload_hash),
        original_payload_hash=str(row.original_payload_hash),
    )


async def fetch_cached_registry_entry(
    redis: Redis,
    session: AsyncSession,
    event_id: UUID,
) -> RedactionRegistryEntry | None:
    raw = await redis.get(redaction_cache_key(event_id))
    if raw is not None:
        data = json.loads(raw.decode() if isinstance(raw, bytes) else str(raw))
        return RedactionRegistryEntry(
            event_id=UUID(data["event_id"]),
            redaction_reason=str(data["redaction_reason"]),
            original_stream=str(data["original_stream"]),
            redacted_payload=dict(data["redacted_payload"]),
            redacted_payload_hash=str(data["redacted_payload_hash"]),
            original_payload_hash=str(data["original_payload_hash"]),
        )
    entry = await fetch_registry_entry(session, event_id)
    if entry is not None:
        await cache_registry_entry(redis, entry)
    return entry


async def cache_registry_entry(redis: Redis, entry: RedactionRegistryEntry) -> None:
    await redis.set(
        redaction_cache_key(entry.event_id),
        json.dumps(
            {
                "event_id": str(entry.event_id),
                "redaction_reason": entry.redaction_reason,
                "original_stream": entry.original_stream,
                "redacted_payload": entry.redacted_payload,
                "redacted_payload_hash": entry.redacted_payload_hash,
                "original_payload_hash": entry.original_payload_hash,
            },
            sort_keys=True,
            default=str,
        ),
        ex=REDACTION_CACHE_TTL_SECONDS,
    )


async def write_registry_entry(
    session: AsyncSession,
    *,
    event_id: UUID,
    redaction_reason: str,
    original_stream: str,
    redacted_payload: dict[str, Any],
    redacted_payload_hash: str,
    original_payload_hash: str,
    redacted_at: datetime | None = None,
) -> None:
    """Call the SECURITY DEFINER function; never direct-INSERT the table.

    The Redis cache is intentionally not primed here: the caller's
    transaction has not yet committed, and a rollback would otherwise
    leave the cache advertising a redaction that the database does not
    have for ``REDACTION_CACHE_TTL_SECONDS``. Consumers populate the
    cache lazily through :func:`fetch_cached_registry_entry`.
    """
    await session.execute(
        text(
            """
            SELECT streams.redaction_registry_insert(
                CAST(:event_id AS UUID),
                :reason,
                :redacted_at,
                :stream,
                CAST(:redacted_payload AS JSONB),
                :redacted_payload_hash,
                :original_payload_hash
            )
            """
        ),
        {
            "event_id": str(event_id),
            "reason": redaction_reason,
            "redacted_at": redacted_at or datetime.now(UTC),
            "stream": original_stream,
            "redacted_payload": json.dumps(redacted_payload, sort_keys=True, default=str),
            "redacted_payload_hash": redacted_payload_hash,
            "original_payload_hash": original_payload_hash,
        },
    )


__all__ = [
    "RedactionRegistryEntry",
    "acquire_event_lock",
    "cache_registry_entry",
    "canonical_payload_hash",
    "fetch_cached_registry_entry",
    "fetch_registry_entry",
    "redaction_cache_key",
    "redaction_lock_key",
    "write_registry_entry",
]
