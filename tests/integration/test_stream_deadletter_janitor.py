"""Dead-letter janitor integration coverage.

Task 17 exercises the four reconciliation passes without driving a real
consumer failure loop, so each test seeds the specific crash-boundary
state it needs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.streams.janitor import stream_deadletter_janitor

pytestmark = pytest.mark.integration


STREAM = "aslan.kap.filings.new"
GROUP = "internal-test"


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _wipe_deadletter_tables(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    yield
    async with session_factory() as session:
        await session.execute(text("DELETE FROM streams.deadletter_xadd_intent"))
        await session.execute(text("DELETE FROM streams.deadletter_redis_index"))
        await session.execute(text("DELETE FROM streams.deadletter_log"))
        await session.commit()


async def test_janitor_pass_2_redrives_stuck_routed_at_redis_null(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fid = await _insert_deadletter_log(
        session,
        event_id="00000000-0000-0000-0000-000000000020",
        original_message_id="20-0",
        routed_age_seconds=600,
    )

    xlen_before = await redis_client.xlen(f"{STREAM}.deadletter")
    await stream_deadletter_janitor(
        redis=redis_client,
        session_factory=session_factory,
        once=True,
    )

    log_row = (
        await session.execute(
            text(
                """
                SELECT redis_message_id, routed_at_redis
                FROM streams.deadletter_log
                WHERE failure_id = :fid
                """
            ),
            {"fid": fid},
        )
    ).one()
    assert log_row.redis_message_id is not None
    assert log_row.routed_at_redis is not None

    idx_rid = (
        await session.execute(
            text(
                """
                SELECT redis_message_id
                FROM streams.deadletter_redis_index
                WHERE failure_id = :fid
                """
            ),
            {"fid": fid},
        )
    ).scalar_one()
    assert idx_rid == log_row.redis_message_id
    assert await redis_client.xlen(f"{STREAM}.deadletter") == xlen_before + 1

    assert (await _audit_count(session, "stream.deadletter", fid)) == 1


async def test_janitor_pass_3_detects_index_orphaned_in_trimmed_stream(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fid = await _insert_deadletter_log(
        session,
        event_id="00000000-0000-0000-0000-000000000030",
        original_message_id="30-0",
        routed_at_redis=True,
        redis_message_id="9999-0",
    )
    await session.execute(
        text(
            """
            INSERT INTO streams.deadletter_redis_index
                (failure_id, redis_message_id)
            VALUES (:fid, '9999-0')
            """
        ),
        {"fid": fid},
    )
    await session.commit()

    await stream_deadletter_janitor(
        redis=redis_client,
        session_factory=session_factory,
        once=True,
    )

    assert (
        await _audit_count(
            session,
            "stream.deadletter_index_orphaned_in_redis",
            fid,
        )
    ) == 1
    idx_count = (
        await session.execute(
            text(
                """
                SELECT count(*)
                FROM streams.deadletter_redis_index
                WHERE failure_id = :fid
                """
            ),
            {"fid": fid},
        )
    ).scalar_one()
    assert idx_count == 1


async def test_janitor_pass_4_reconciles_recent_orphan_without_intent(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fid = await _insert_deadletter_log(
        session,
        event_id="00000000-0000-0000-0000-000000000040",
        original_message_id="40-0",
    )
    orphan_message_id = await redis_client.xadd(
        f"{STREAM}.deadletter",
        {
            "event_id": "00000000-0000-0000-0000-000000000040",
            "failure_id": str(fid),
            "stream_name": STREAM,
        },
    )
    xlen_before = await redis_client.xlen(f"{STREAM}.deadletter")

    await stream_deadletter_janitor(
        redis=redis_client,
        session_factory=session_factory,
        pass4_window=10_000,
        once=True,
    )

    log_row = (
        await session.execute(
            text(
                """
                SELECT redis_message_id, routed_at_redis
                FROM streams.deadletter_log
                WHERE failure_id = :fid
                """
            ),
            {"fid": fid},
        )
    ).one()
    assert log_row.redis_message_id == orphan_message_id
    assert log_row.routed_at_redis is not None
    assert await redis_client.xlen(f"{STREAM}.deadletter") == xlen_before
    assert await _audit_count(session, "stream.deadletter_orphan_reconciled", fid) == 1


async def test_janitor_pass_4_xdels_duplicate_orphan_when_failure_id_indexed(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fid = await _insert_deadletter_log(
        session,
        event_id="00000000-0000-0000-0000-000000000045",
        original_message_id="45-0",
        routed_at_redis=True,
        redis_message_id="45-0",
    )
    canonical_message_id = await redis_client.xadd(
        f"{STREAM}.deadletter",
        {"event_id": "00000000-0000-0000-0000-000000000045", "failure_id": str(fid)},
    )
    await session.execute(
        text(
            """
            UPDATE streams.deadletter_log
            SET redis_message_id = :rid
            WHERE failure_id = :fid
            """
        ),
        {"fid": fid, "rid": canonical_message_id},
    )
    await session.execute(
        text(
            """
            INSERT INTO streams.deadletter_redis_index
                (failure_id, redis_message_id)
            VALUES (:fid, :rid)
            """
        ),
        {"fid": fid, "rid": canonical_message_id},
    )
    await session.commit()
    duplicate_message_id = await redis_client.xadd(
        f"{STREAM}.deadletter",
        {"event_id": "00000000-0000-0000-0000-000000000045", "failure_id": str(fid)},
    )

    await stream_deadletter_janitor(
        redis=redis_client,
        session_factory=session_factory,
        pass4_window=10_000,
        once=True,
    )

    assert await redis_client.xrange(
        f"{STREAM}.deadletter",
        min=canonical_message_id,
        max=canonical_message_id,
    )
    assert not await redis_client.xrange(
        f"{STREAM}.deadletter",
        min=duplicate_message_id,
        max=duplicate_message_id,
    )
    assert await _audit_count(session, "stream.deadletter_orphan_xdel", fid) == 1


async def test_janitor_pass_1_marks_lost_when_stale_intent_has_no_redis_entry(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fid = await _insert_deadletter_log(
        session,
        event_id="00000000-0000-0000-0000-000000000050",
        original_message_id="50-0",
    )
    await session.execute(
        text(
            """
            INSERT INTO streams.deadletter_xadd_intent (
                failure_id, stream_name, intent_at, redis_lower_bound_id,
                owner_id, owner_heartbeat_at
            ) VALUES (
                :fid, :stream, now() - INTERVAL '10 minutes', '0-0',
                'consumer:dead', now() - INTERVAL '2 minutes'
            )
            """
        ),
        {"fid": fid, "stream": STREAM},
    )
    await session.commit()

    await stream_deadletter_janitor(
        redis=redis_client,
        session_factory=session_factory,
        once=True,
    )

    assert await _audit_count(session, "stream.deadletter_orphan_lost", fid) == 1
    assert (
        await session.execute(
            text(
                """
                SELECT count(*)
                FROM streams.deadletter_xadd_intent
                WHERE failure_id = :fid
                """
            ),
            {"fid": fid},
        )
    ).scalar_one() == 0


async def _insert_deadletter_log(
    session: AsyncSession,
    *,
    event_id: str,
    original_message_id: str,
    routed_age_seconds: int = 0,
    routed_at_redis: bool = False,
    redis_message_id: str | None = None,
) -> int:
    result = await session.execute(
        text(
            """
            INSERT INTO streams.deadletter_log (
                stream_name, deadletter_stream, event_id,
                original_message_id, group_name, consumer_name,
                failure_count, last_error, payload_excerpt, routed_at,
                routed_at_redis, redis_message_id
            ) VALUES (
                :stream, :deadletter_stream, :event_id,
                :original_message_id, :group_name, 'consumer:test',
                5, 'boom', '{}'::jsonb,
                now() - (:routed_age_seconds * INTERVAL '1 second'),
                CASE WHEN :routed_at_redis THEN now() ELSE NULL END,
                :redis_message_id
            )
            RETURNING failure_id
            """
        ),
        {
            "stream": STREAM,
            "deadletter_stream": f"{STREAM}.deadletter",
            "event_id": event_id,
            "original_message_id": original_message_id,
            "group_name": GROUP,
            "routed_age_seconds": routed_age_seconds,
            "routed_at_redis": routed_at_redis,
            "redis_message_id": redis_message_id,
        },
    )
    await session.commit()
    return int(result.scalar_one())


async def _audit_count(
    session: AsyncSession,
    operation: str,
    failure_id: int,
) -> int:
    return int(
        (
            await session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM audit.events
                    WHERE operation = :operation
                      AND target_pk @> jsonb_build_object(
                          'failure_id', CAST(:fid AS BIGINT)
                      )
                    """
                ),
                {"operation": operation, "fid": failure_id},
            )
        ).scalar_one()
    )
