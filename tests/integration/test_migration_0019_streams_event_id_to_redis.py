"""Migration 0019 — streams.event_id_to_redis (v0.5.0 Task 5c).

Codex F3. Drainer-populated index from ``event_id`` to its in-Redis
stream + message_id, used by the redaction runtime in Task 18 to find
in-Redis copies that need XDEL. Composite PK
``(event_id, stream_name, redis_message_id)`` because a single event_id
can land in MULTIPLE Redis stream entries (drainer-retry; future
fan-out).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_event_id_to_redis_columns(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema='streams' "
                    "AND table_name='event_id_to_redis'"
                )
            )
        ).all()
    cols: dict[str, tuple[str, str]] = {r[0]: (r[1], r[2]) for r in rows}
    assert cols["event_id"] == ("uuid", "NO")
    assert cols["stream_name"] == ("text", "NO")
    assert cols["redis_message_id"] == ("text", "NO")
    assert cols["published_at"] == ("timestamp with time zone", "NO")
    assert cols["redacted_at"] == ("timestamp with time zone", "YES")


@pytest.mark.asyncio(loop_scope="session")
async def test_event_id_to_redis_composite_pk(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        pk_rows = (
            await conn.execute(
                text(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid=i.indrelid "
                    "AND a.attnum=ANY(i.indkey) "
                    "WHERE i.indrelid='streams.event_id_to_redis'::regclass "
                    "AND i.indisprimary "
                    "ORDER BY array_position(i.indkey, a.attnum)"
                )
            )
        ).all()
    assert [r[0] for r in pk_rows] == [
        "event_id",
        "stream_name",
        "redis_message_id",
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_event_id_to_redis_pending_partial_index(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname='streams' "
                "AND indexname='event_id_to_redis_pending'"
            )
        )
        defn = result.scalar_one()
    assert "WHERE" in defn.upper()
    assert "redacted_at IS NULL" in defn


@pytest.mark.asyncio(loop_scope="session")
async def test_event_id_to_redis_event_id_index(engine: AsyncEngine) -> None:
    """Lookup-by-event_id index used by the redaction runtime to find
    every Redis stream entry for a given event."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname='streams' "
                    "AND tablename='event_id_to_redis'"
                )
            )
        ).all()
    names = {r[0] for r in rows}
    assert "event_id_to_redis_event_id" in names
