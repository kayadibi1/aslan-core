"""Migration 0025 — drop global UNIQUE(redis_message_id) on
streams.deadletter_redis_index.

The constraint introduced by migration 0017 mistakenly assumed Redis
stream message ids are globally unique. They are not: two distinct
deadletter streams XADDing inside the same millisecond can each return
``<ms>-0``. The constraint then misclassified cross-stream collisions
as same-``failure_id`` races and caused silent dead-letter loss +
consumer crashes.

This test file (a) confirms the constraint is gone after 0025 applies,
and (b) confirms two ``deadletter_redis_index`` rows with different
``failure_id`` values can share a ``redis_message_id`` — the exact
shape that the v0.5.0 routing code now needs to tolerate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _wipe_deadletter(session: AsyncSession) -> AsyncIterator[None]:
    yield
    for stmt in [
        "DELETE FROM streams.deadletter_xadd_intent",
        "DELETE FROM streams.deadletter_redis_index",
        "DELETE FROM streams.deadletter_log",
    ]:
        await session.execute(text(stmt))
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_no_global_unique_on_redis_message_id(engine: AsyncEngine) -> None:
    """No table-wide UNIQUE on ``redis_message_id`` may remain after 0025."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.conname, "
                    "  (SELECT array_agg(a.attname ORDER BY a.attnum) "
                    "   FROM unnest(c.conkey) k "
                    "   JOIN pg_attribute a "
                    "     ON a.attrelid = c.conrelid AND a.attnum = k) AS cols "
                    "FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "JOIN pg_namespace n ON n.oid = t.relnamespace "
                    "WHERE n.nspname = 'streams' "
                    "  AND t.relname = 'deadletter_redis_index' "
                    "  AND c.contype = 'u'"
                )
            )
        ).all()
    offending = [r for r in rows if r.cols == ["redis_message_id"]]
    assert offending == [], (
        f"migration 0025 should have dropped the global UNIQUE "
        f"on redis_message_id; still present as: {offending}"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_distinct_failure_ids_may_share_redis_message_id(
    engine: AsyncEngine,
) -> None:
    """Two deadletter routes whose XADDs collide on ``<ms>-<seq>`` must
    both persist without the index INSERT raising. This is the exact
    shape that the v0.5.0 routing helper at ``streams/deadletter.py``
    used to misclassify as a same-``failure_id`` race."""
    shared_rid = "1700000000000-0"
    async with engine.begin() as conn:
        for tag in ("collision-a", "collision-b"):
            await conn.execute(
                text(
                    "INSERT INTO streams.deadletter_log "
                    "(stream_name, deadletter_stream, event_id, "
                    " original_message_id, group_name, consumer_name, "
                    " failure_count, last_error) "
                    "VALUES (:s, :ds, gen_random_uuid(), :omid, "
                    "        'g', 'c', 5, 'err')"
                ),
                {
                    "s": f"src.{tag}",
                    "ds": f"src.{tag}.deadletter",
                    "omid": f"omid-{tag}",
                },
            )
        fids = (
            (
                await conn.execute(
                    text(
                        "SELECT failure_id FROM streams.deadletter_log "
                        "WHERE original_message_id IN ('omid-collision-a', "
                        "                              'omid-collision-b') "
                        "ORDER BY failure_id"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(fids) == 2

        for fid in fids:
            await conn.execute(
                text(
                    "INSERT INTO streams.deadletter_redis_index "
                    "(failure_id, redis_message_id) "
                    "VALUES (:fid, :rid)"
                ),
                {"fid": fid, "rid": shared_rid},
            )

        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT failure_id FROM streams.deadletter_redis_index "
                        "WHERE redis_message_id = :rid "
                        "ORDER BY failure_id"
                    ),
                    {"rid": shared_rid},
                )
            )
            .scalars()
            .all()
        )
    assert rows == fids


@pytest.mark.asyncio(loop_scope="session")
async def test_same_failure_id_still_blocked_by_pk(engine: AsyncEngine) -> None:
    """The PK on ``failure_id`` still prevents a same-``failure_id``
    double insert — that's the only legitimate race the v0.5.0 routing
    helper guards against, and it must keep working post-0025."""
    from sqlalchemy.exc import IntegrityError

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO streams.deadletter_log "
                "(stream_name, deadletter_stream, event_id, "
                " original_message_id, group_name, consumer_name, "
                " failure_count, last_error) "
                "VALUES ('s', 's.deadletter', gen_random_uuid(), "
                "        'omid-pk-guard', 'g', 'c', 5, 'err')"
            )
        )
        fid = (
            await conn.execute(
                text(
                    "SELECT failure_id FROM streams.deadletter_log "
                    "WHERE original_message_id = 'omid-pk-guard'"
                )
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO streams.deadletter_redis_index "
                "(failure_id, redis_message_id) VALUES (:fid, :rid)"
            ),
            {"fid": fid, "rid": "rid-first"},
        )

    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO streams.deadletter_redis_index "
                    "(failure_id, redis_message_id) VALUES (:fid, :rid)"
                ),
                {"fid": fid, "rid": "rid-second"},
            )
