"""``/deadletter`` issues O(unique-streams) Redis calls regardless of row count.

Spec §5.3 + §8.2: the deadletter page renders the same number of
Redis ops whether the table holds 10 rows or 1000. Per-row probes
would multiply under pagination + polling and blow the budget; the
implementation collapses to per-unique-deadletter-stream probes
under a ``1 + len(STREAMS)`` budget.

This test seeds 1000 dead-letter rows pointing at one
deadletter_stream, hits ``/deadletter``, and asserts the
instrumented Redis client recorded ≤ ``1 + len(STREAMS) * 2``
calls — the budget the page handler opens. The factor-of-2
slack accommodates a future ``xinfo_stream_lite + xpending`` per
stream; the contract is that the count is bounded by stream
count, not row count.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.dashboard.app import app, configure_app
from aslan_core.streams.names import STREAMS

pytestmark = pytest.mark.integration


class _CountingRedis:
    """Pass-through wrapper around redis.asyncio.Redis that counts
    every coroutine call the dashboard's probe layer makes."""

    def __init__(self, inner: Redis) -> None:
        self._inner = inner
        self.calls: int = 0

    async def xlen(self, name: str) -> int:
        self.calls += 1
        result = await cast("Any", self._inner).xlen(name)
        return int(result)

    async def xpending(self, name: str, group: str) -> Any:
        self.calls += 1
        return await self._inner.xpending(name, group)

    async def xinfo_stream(self, name: str) -> Any:
        self.calls += 1
        return await self._inner.xinfo_stream(name)

    async def scard(self, name: str) -> int:
        self.calls += 1
        result = await cast("Any", self._inner).scard(name)
        return int(result)


@pytest_asyncio.fixture(loop_scope="session")
async def _seeded_deadletter(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Seed 1000 dead-letter rows. The fixture cleans up on teardown
    even if the test fails."""
    failure_ids: list[int] = []
    for i in range(1000):
        fid = (
            await session.execute(
                text(
                    "INSERT INTO streams.deadletter_log "
                    "(stream_name, deadletter_stream, event_id, "
                    " original_message_id, group_name, consumer_name, "
                    " failure_count, last_error) "
                    "VALUES('kap', 'kap__deadletter', gen_random_uuid(), "
                    "       :mid, 'g', 'c', 1, 'err') "
                    "RETURNING failure_id"
                ),
                {"mid": f"{i}-0"},
            )
        ).scalar_one()
        failure_ids.append(int(fid))
    await session.commit()
    try:
        yield
    finally:
        async with session_factory() as s:
            await s.execute(text("DELETE FROM streams.deadletter_log"))
            await s.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_deadletter_redis_calls_are_bounded_by_stream_count(
    _seeded_deadletter: None,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    counting = _CountingRedis(redis_client)
    configure_app(
        session_factory=session_factory,
        redis_client=counting,  # type: ignore[arg-type]
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/deadletter")
    assert response.status_code == 200

    cap = 1 + len(STREAMS) * 2
    assert counting.calls <= cap, (
        f"deadletter page issued {counting.calls} Redis calls; cap is "
        f"1 + len(STREAMS) * 2 = {cap}. Per-row probes have re-entered "
        "the page handler — see spec §5.3."
    )
