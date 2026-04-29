"""Migration 0017 — deadletter_log + deadletter_redis_index +
deadletter_xadd_intent (v0.5.0 Task 5)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_deadletter_log_columns(engine: AsyncEngine) -> None:
    expected = {
        "failure_id": "bigint",
        "stream_name": "text",
        "deadletter_stream": "text",
        "event_id": "uuid",
        "original_message_id": "text",
        "group_name": "text",
        "consumer_name": "text",
        "failure_count": "integer",
        "last_error": "text",
        "payload_excerpt": "jsonb",
        "routed_at": "timestamp with time zone",
        "routed_at_redis": "timestamp with time zone",
        "redis_message_id": "text",
        "actor_id": "text",
        "actor_kind": "text",
        "request_id": "uuid",
    }
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema='streams' AND table_name='deadletter_log'"
                )
            )
        ).all()
    actual: dict[str, str] = {r[0]: r[1] for r in rows}
    for name, dtype in expected.items():
        assert name in actual, f"missing column {name}"
        assert actual[name] == dtype, f"{name}: {actual[name]} != {dtype}"


@pytest.mark.asyncio(loop_scope="session")
async def test_deadletter_log_unique_routing_constraint(engine: AsyncEngine) -> None:
    """UNIQUE (stream_name, group_name, original_message_id) — the F2 +
    F18 dedup anchor."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE c.conname = 'deadletter_routing_uq' "
                "AND t.relname = 'deadletter_log'"
            )
        )
        defn = result.scalar_one_or_none()
    assert defn is not None
    assert "stream_name" in defn
    assert "group_name" in defn
    assert "original_message_id" in defn


@pytest.mark.asyncio(loop_scope="session")
async def test_deadletter_pending_redis_partial_index(engine: AsyncEngine) -> None:
    """Partial-failure recovery: index on rows with NULL routed_at_redis."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname='streams' "
                "AND indexname='deadletter_pending_redis'"
            )
        )
        defn = result.scalar_one()
    assert "WHERE" in defn.upper()
    assert "routed_at_redis" in defn


@pytest.mark.asyncio(loop_scope="session")
async def test_deadletter_redis_index_present(engine: AsyncEngine) -> None:
    """Codex F9 round 4 — sister table for O(1) Postgres-side recovery
    keyed on failure_id."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema='streams' "
                    "AND table_name='deadletter_redis_index'"
                )
            )
        ).all()
    cols: dict[str, str] = {r[0]: r[1] for r in rows}
    assert cols.get("failure_id") == "bigint"
    assert cols.get("redis_message_id") == "text"
    # PK on failure_id
    async with engine.connect() as conn:
        pk_rows = (
            await conn.execute(
                text(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid=i.indrelid "
                    "AND a.attnum=ANY(i.indkey) "
                    "WHERE i.indrelid='streams.deadletter_redis_index'::regclass "
                    "AND i.indisprimary"
                )
            )
        ).all()
    assert {r[0] for r in pk_rows} == {"failure_id"}
    # UNIQUE on redis_message_id
    async with engine.connect() as conn:
        uniques = (
            await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid=c.conrelid "
                    "WHERE t.relname='deadletter_redis_index' "
                    "AND c.contype='u'"
                )
            )
        ).all()
    assert any("redis_message_id" in r[0] for r in uniques)


@pytest.mark.asyncio(loop_scope="session")
async def test_deadletter_xadd_intent_columns(engine: AsyncEngine) -> None:
    """Codex F14 round 6 + F17 round 7 + F20 round 9 — pre-XADD intent
    cursor with Redis-ID lower bound + owner identity + heartbeat."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema='streams' "
                    "AND table_name='deadletter_xadd_intent'"
                )
            )
        ).all()
    cols: dict[str, tuple[str, str]] = {r[0]: (r[1], r[2]) for r in rows}
    assert cols["failure_id"] == ("bigint", "NO")
    assert cols["stream_name"] == ("text", "NO")
    assert cols["intent_at"] == ("timestamp with time zone", "NO")
    assert cols["redis_lower_bound_id"] == ("text", "NO")
    assert cols["owner_id"] == ("text", "NO")
    assert cols["owner_heartbeat_at"] == ("timestamp with time zone", "NO")


@pytest.mark.asyncio(loop_scope="session")
async def test_deadletter_xadd_intent_indexes(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname='streams' "
                    "AND tablename='deadletter_xadd_intent'"
                )
            )
        ).all()
    names = {r[0] for r in rows}
    assert "deadletter_xadd_intent_stale" in names
    assert "deadletter_xadd_intent_heartbeat" in names


@pytest.mark.asyncio(loop_scope="session")
async def test_deadletter_xadd_intent_cascades_on_log_delete(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    """ON DELETE CASCADE from the log row — DBA cleanup paths shouldn't
    leak intent rows."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO streams.deadletter_log "
                "(stream_name, deadletter_stream, event_id, original_message_id, "
                " group_name, consumer_name, failure_count, last_error) "
                "VALUES ('s', 's.deadletter', gen_random_uuid(), 'rid-cascade-1', "
                "        'g', 'c', 5, 'err')"
            )
        )
        result = await conn.execute(
            text(
                "SELECT failure_id FROM streams.deadletter_log "
                "WHERE original_message_id='rid-cascade-1'"
            )
        )
        fid = result.scalar_one()
        await conn.execute(
            text(
                "INSERT INTO streams.deadletter_xadd_intent "
                "(failure_id, stream_name, redis_lower_bound_id, owner_id) "
                "VALUES (:fid, 's', '0-0', 'host:1:abc')"
            ),
            {"fid": fid},
        )
        await conn.execute(
            text("DELETE FROM streams.deadletter_log WHERE failure_id=:fid"),
            {"fid": fid},
        )
        rows = (
            await conn.execute(
                text("SELECT 1 FROM streams.deadletter_xadd_intent WHERE failure_id=:fid"),
                {"fid": fid},
            )
        ).all()
    assert rows == []


@pytest.mark.asyncio(loop_scope="session")
async def test_deadletter_redis_index_cascades_on_log_delete(
    engine: AsyncEngine,
) -> None:
    """The redis_index sister also cascades — keeps Postgres-side state
    self-cleaning on log deletion."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO streams.deadletter_log "
                "(stream_name, deadletter_stream, event_id, original_message_id, "
                " group_name, consumer_name, failure_count, last_error) "
                "VALUES ('s', 's.deadletter', gen_random_uuid(), 'rid-cascade-2', "
                "        'g', 'c', 5, 'err')"
            )
        )
        fid = (
            await conn.execute(
                text(
                    "SELECT failure_id FROM streams.deadletter_log "
                    "WHERE original_message_id='rid-cascade-2'"
                )
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO streams.deadletter_redis_index "
                "(failure_id, redis_message_id) "
                "VALUES (:fid, 'rmsg-cascade-2')"
            ),
            {"fid": fid},
        )
        await conn.execute(
            text("DELETE FROM streams.deadletter_log WHERE failure_id=:fid"),
            {"fid": fid},
        )
        rows = (
            await conn.execute(
                text("SELECT 1 FROM streams.deadletter_redis_index WHERE failure_id=:fid"),
                {"fid": fid},
            )
        ).all()
    assert rows == []
