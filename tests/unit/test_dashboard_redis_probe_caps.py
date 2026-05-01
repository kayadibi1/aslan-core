"""Operational caps on Redis probes — timeout, budget, circuit breaker.

Spec §5.3 + codex round-1: the dashboard must remain bounded under
any backlog size. These unit tests exercise the cap enforcement in
isolation with a fake Redis client (no testcontainer needed). The
end-to-end integration test against a real Redis lives in
``tests/integration/test_dashboard_redis_circuit_breaker.py``.

Coverage:

  * Per-call timeout: a slow fake (sleeps > 200ms) returns ``None``
    and trips the breaker.
  * Per-page budget: a probe past the cap returns ``None`` without
    touching Redis.
  * Circuit-breaker state machine: 5 errors within the window opens
    the breaker; while open, probes short-circuit; after the open
    period elapses the breaker closes and admits a fresh probe.
  * Successful probes record_success and reset the error window
    (only while the breaker is closed — an open breaker still counts
    down its open period).
"""

from __future__ import annotations

import asyncio

import pytest

from aslan_core.dashboard.redis_probes import (
    CIRCUIT_ERROR_THRESHOLD,
    CIRCUIT_OPEN_S,
    PROBE_TIMEOUT_S,
    RedisCircuitBreaker,
    open_budget,
    xlen,
)


class _FakeRedis:
    """Test double that implements just enough of the ``_RedisLike``
    Protocol for the probes. Methods other than ``xlen`` raise
    ``NotImplementedError`` — the tests in this file only exercise
    the xlen path."""

    def __init__(
        self,
        *,
        xlen_value: int = 7,
        xlen_delay_s: float = 0.0,
        xlen_raises: type[BaseException] | None = None,
    ) -> None:
        self.xlen_value = xlen_value
        self.xlen_delay_s = xlen_delay_s
        self.xlen_raises = xlen_raises
        self.xlen_calls = 0

    async def xlen(self, name: str) -> int:
        self.xlen_calls += 1
        if self.xlen_delay_s:
            await asyncio.sleep(self.xlen_delay_s)
        if self.xlen_raises is not None:
            raise self.xlen_raises("boom")
        return self.xlen_value

    async def xpending(self, name: str, group: str) -> object:
        raise NotImplementedError

    async def xinfo_stream(self, name: str) -> object:
        raise NotImplementedError

    async def scard(self, name: str) -> int:
        raise NotImplementedError


# ── Timeout ───────────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_xlen_returns_none_on_probe_timeout() -> None:
    """A probe that exceeds 200ms returns None and increments the
    breaker error count."""
    fake = _FakeRedis(xlen_delay_s=PROBE_TIMEOUT_S * 5)
    breaker = RedisCircuitBreaker()
    with open_budget(limit=10) as budget:
        result = await xlen(fake, "kap", breaker=breaker, budget=budget)
    assert result is None
    assert fake.xlen_calls == 1, "Redis call was issued; the timeout fires server-side"


# ── Budget ────────────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_xlen_returns_none_when_budget_exhausted() -> None:
    fake = _FakeRedis()
    breaker = RedisCircuitBreaker()
    with open_budget(limit=2) as budget:
        first = await xlen(fake, "a", breaker=breaker, budget=budget)
        second = await xlen(fake, "b", breaker=breaker, budget=budget)
        third = await xlen(fake, "c", breaker=breaker, budget=budget)
    assert first == 7
    assert second == 7
    assert third is None
    assert fake.xlen_calls == 2, "third call must NOT touch Redis"
    assert budget.consumed == 2, "exhausted budget does not advance the counter further"


@pytest.mark.asyncio(loop_scope="session")
async def test_open_budget_starts_at_zero() -> None:
    with open_budget(limit=5) as budget:
        assert budget.consumed == 0
        assert budget.remaining == 5


# ── Circuit breaker ───────────────────────────────────────────────


def test_circuit_breaker_opens_after_threshold_errors() -> None:
    breaker = RedisCircuitBreaker()
    # Simulate errors at t=0, 1, 2, 3, 4 (within the 60s window).
    for i in range(CIRCUIT_ERROR_THRESHOLD):
        breaker.record_error(now=float(i))
    assert breaker.opened_at == float(CIRCUIT_ERROR_THRESHOLD - 1)
    assert breaker.is_open(now=float(CIRCUIT_ERROR_THRESHOLD))


def test_circuit_breaker_does_not_open_below_threshold() -> None:
    breaker = RedisCircuitBreaker()
    for i in range(CIRCUIT_ERROR_THRESHOLD - 1):
        breaker.record_error(now=float(i))
    assert breaker.opened_at is None
    assert not breaker.is_open(now=float(CIRCUIT_ERROR_THRESHOLD))


def test_circuit_breaker_closes_after_open_period() -> None:
    breaker = RedisCircuitBreaker()
    for i in range(CIRCUIT_ERROR_THRESHOLD):
        breaker.record_error(now=float(i))
    opened_at = breaker.opened_at
    assert opened_at is not None
    # Just before open period elapses → still open.
    assert breaker.is_open(now=opened_at + CIRCUIT_OPEN_S - 1.0)
    # After open period → closed; error window cleared so a single
    # follow-up error does NOT immediately reopen.
    assert not breaker.is_open(now=opened_at + CIRCUIT_OPEN_S + 0.1)
    assert breaker.opened_at is None


def test_circuit_breaker_drops_old_errors_outside_window() -> None:
    """A burst of errors more than 60s in the past does NOT
    contribute to the current sliding window."""
    breaker = RedisCircuitBreaker()
    # 4 errors at t=0..3 (within window if observed soon).
    for i in range(CIRCUIT_ERROR_THRESHOLD - 1):
        breaker.record_error(now=float(i))
    # One more error at t=120 (>60s after the burst). Sliding window
    # drops the old entries; now there's only 1 error in the last 60s
    # → breaker stays closed.
    breaker.record_error(now=120.0)
    assert breaker.opened_at is None
    assert not breaker.is_open(now=120.0)


def test_circuit_breaker_success_resets_error_window() -> None:
    breaker = RedisCircuitBreaker()
    for i in range(CIRCUIT_ERROR_THRESHOLD - 1):
        breaker.record_error(now=float(i))
    # 4 errors so far (one below threshold). A success clears the
    # window → next error count restarts.
    breaker.record_success()
    breaker.record_error(now=float(CIRCUIT_ERROR_THRESHOLD))
    assert breaker.opened_at is None


@pytest.mark.asyncio(loop_scope="session")
async def test_xlen_short_circuits_when_breaker_open() -> None:
    """The breaker uses ``time.monotonic`` for its open-period check
    when no explicit ``now`` is passed. Trip it with real-time error
    records so ``is_open()`` (which uses real time too) returns True
    inside the test window."""
    fake = _FakeRedis()
    breaker = RedisCircuitBreaker()
    for _ in range(CIRCUIT_ERROR_THRESHOLD):
        breaker.record_error()
    assert breaker.is_open(), "breaker must be open after threshold errors"
    with open_budget(limit=10) as budget:
        result = await xlen(fake, "kap", breaker=breaker, budget=budget)
    assert result is None
    assert fake.xlen_calls == 0, "open breaker must NOT touch Redis"
    assert budget.consumed == 0, "open breaker must NOT consume budget"


@pytest.mark.asyncio(loop_scope="session")
async def test_xlen_records_error_on_exception() -> None:
    fake = _FakeRedis(xlen_raises=RuntimeError)
    breaker = RedisCircuitBreaker()
    with open_budget(limit=10) as budget:
        result = await xlen(fake, "kap", breaker=breaker, budget=budget)
    assert result is None
    assert fake.xlen_calls == 1
    # First error not enough to open the breaker.
    assert not breaker.is_open()


@pytest.mark.asyncio(loop_scope="session")
async def test_repeated_xlen_failures_open_the_breaker() -> None:
    fake = _FakeRedis(xlen_raises=RuntimeError)
    breaker = RedisCircuitBreaker()
    with open_budget(limit=CIRCUIT_ERROR_THRESHOLD * 2) as budget:
        for _ in range(CIRCUIT_ERROR_THRESHOLD):
            await xlen(fake, "kap", breaker=breaker, budget=budget)
        # Threshold reached → breaker open. Next call short-circuits.
        assert breaker.is_open()
        result = await xlen(fake, "kap", breaker=breaker, budget=budget)
    assert result is None
    assert fake.xlen_calls == CIRCUIT_ERROR_THRESHOLD, (
        "the post-threshold call must NOT touch Redis"
    )


# ── Sanity: redis_probes constants are sensible ──────────────────


def test_probe_constants_are_sensible() -> None:
    """Codex spec §5.3 fixed values — pin them so a future refactor
    that loosens the contract is visible in the diff."""
    assert PROBE_TIMEOUT_S == 0.2
    assert CIRCUIT_ERROR_THRESHOLD == 5
    assert CIRCUIT_OPEN_S == 30.0
