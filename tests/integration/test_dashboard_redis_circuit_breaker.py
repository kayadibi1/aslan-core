"""End-to-end Redis-probe circuit breaker against a real Redis.

Spec §5.3 + codex round-1: the dashboard must remain bounded under
any backlog size. This test seeds a Redis stream with 1000 entries
and verifies that:

  * ``xlen`` against a real stream returns the correct count.
  * ``xinfo_stream_lite`` returns the expected length + last-entry
    id without walking consumers.
  * Forcing 5 errors via a deliberately-broken Redis client opens the
    breaker; the 7th attempt short-circuits without touching Redis.

Cap enforcement against a fake Redis lives in
``tests/unit/test_dashboard_redis_probe_caps.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from aslan_core.dashboard.redis_probes import (
    CIRCUIT_ERROR_THRESHOLD,
    RedisCircuitBreaker,
    open_budget,
    xinfo_stream_lite,
    xlen,
)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(loop_scope="session")
async def _seeded_stream(redis_client: Redis) -> AsyncIterator[str]:
    """Seed a real Redis stream with 1000 small entries so the
    bounded-probe contract is exercised against a non-trivial
    backlog. ``redis_client`` already flushes on entry/exit so
    cleanup is automatic."""
    stream = "test:dashboard:probes"
    pipe = redis_client.pipeline()
    for i in range(1000):
        pipe.xadd(stream, {"i": str(i)})
    await pipe.execute()
    yield stream


@pytest.mark.asyncio(loop_scope="session")
async def test_xlen_against_real_redis_returns_seeded_count(
    redis_client: Redis,
    _seeded_stream: str,
) -> None:
    breaker = RedisCircuitBreaker()
    # ``redis.asyncio.Redis``'s wider signature (``Awaitable[T] | T``,
    # accepts ``bytes | str | memoryview``) doesn't structurally match
    # the narrow ``_RedisLike`` Protocol the probes declare. The
    # type-ignore here reflects the runtime contract — the real client
    # exhibits the methods the Protocol requires.
    with open_budget(limit=4) as budget:
        result = await xlen(redis_client, _seeded_stream, breaker=breaker, budget=budget)  # type: ignore[arg-type]
    assert result == 1000


@pytest.mark.asyncio(loop_scope="session")
async def test_xinfo_stream_lite_returns_length_and_last_entry(
    redis_client: Redis,
    _seeded_stream: str,
) -> None:
    breaker = RedisCircuitBreaker()
    with open_budget(limit=4) as budget:
        info = await xinfo_stream_lite(redis_client, _seeded_stream, breaker=breaker, budget=budget)  # type: ignore[arg-type]
    assert info is not None
    assert info["length"] == 1000
    assert info["last_entry_id"] is not None


class _BrokenRedis:
    """Redis stub that raises on every call. Drives the breaker
    state machine without needing to take down the real testcontainer.

    Implements the full ``_RedisLike`` Protocol; every method raises
    so the breaker counts each probe as an error."""

    async def xlen(self, name: str) -> int:
        raise RuntimeError("simulated redis outage")

    async def xpending(self, name: str, group: str) -> object:
        raise RuntimeError("simulated redis outage")

    async def xinfo_stream(self, name: str) -> object:
        raise RuntimeError("simulated redis outage")

    async def scard(self, name: str) -> int:
        raise RuntimeError("simulated redis outage")


@pytest.mark.asyncio(loop_scope="session")
async def test_repeated_failures_open_breaker_against_real_dashboard_flow() -> None:
    """End-to-end: 5 consecutive probe failures open the breaker;
    the 6th call short-circuits without invoking the (broken) Redis.
    Mirrors the real dashboard render path where each page allocates
    a fresh budget but the breaker is shared across requests."""
    broken = _BrokenRedis()
    breaker = RedisCircuitBreaker()

    # 5 failed probes — last one trips the breaker.
    with open_budget(limit=CIRCUIT_ERROR_THRESHOLD) as budget:
        for _ in range(CIRCUIT_ERROR_THRESHOLD):
            result = await xlen(broken, "kap", breaker=breaker, budget=budget)
            assert result is None
    assert breaker.is_open(), "breaker must be open after threshold errors"

    # New page render → fresh budget. The probe must short-circuit.
    with open_budget(limit=10) as budget:
        result = await xlen(broken, "kap", breaker=breaker, budget=budget)
    assert result is None
    assert budget.consumed == 0, "open breaker must NOT consume the new budget"
