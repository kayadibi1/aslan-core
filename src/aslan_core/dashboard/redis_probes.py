"""Bounded Redis probes for the v0.6.0 dashboard.

Spec §5.3 + codex round-1: every Redis op the dashboard issues is
O(1) per stream — ``XLEN``, ``XPENDING`` (summary only),
``XINFO STREAM`` (length + last-entry only), ``SCARD``. ``SCAN``,
``KEYS``, and ``XRANGE`` are forbidden because they multiply under
pagination + polling.

Hard caps:

  * **Per-call timeout: 200 ms.** Enforced via ``asyncio.wait_for``
    around every Redis coroutine. A probe that exceeds it returns
    ``None`` (the page renders an ``unknown`` cell).
  * **Per-page call budget:** ``1 + len(STREAMS)`` by default — set
    when the page handler enters the budget context. The deadletter
    page MUST NOT loop over rows issuing per-row probes; the budget
    refuses any call beyond the cap, returning ``None``.
  * **Circuit breaker:** 5 errors within a 60s sliding window opens
    the breaker for 30s. While open, every probe short-circuits to
    ``None`` without touching the network. State lives in-process
    on the FastHTML app instance — no shared cache, no cross-process
    coordination needed for v0.6.0's operator-scale traffic.

The probe functions are independent of the FastHTML app — they take
the breaker + budget + Redis client as explicit args so unit tests
can drive them with fakes.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator


class _RedisLike(Protocol):
    """The minimal Redis-async surface the probes need.

    Declared as a Protocol so the probes accept any duck-typed
    client — the real ``redis.asyncio.Redis``, a test fake, or a
    deliberately-broken stub for circuit-breaker tests. ``Awaitable``
    return types reflect what the probe wrappers ``await``."""

    async def xlen(self, name: str) -> int: ...

    async def xpending(self, name: str, group: str) -> Any: ...

    async def xinfo_stream(self, name: str) -> Any: ...

    async def scard(self, name: str) -> int: ...


PROBE_TIMEOUT_S: float = 0.2
"""Per-call socket timeout. Probes that exceed this return ``None``."""

CIRCUIT_ERROR_THRESHOLD: int = 5
"""Errors within ``CIRCUIT_WINDOW_S`` that trip the breaker."""

CIRCUIT_WINDOW_S: float = 60.0
"""Sliding window over which errors are counted."""

CIRCUIT_OPEN_S: float = 30.0
"""Time the breaker stays open before a probe is retried."""


# ── Circuit breaker ────────────────────────────────────────────────


@dataclass
class RedisCircuitBreaker:
    """Per-app-instance circuit breaker for Redis probes.

    Holds a sliding window of error timestamps. Once
    ``CIRCUIT_ERROR_THRESHOLD`` errors land within
    ``CIRCUIT_WINDOW_S``, ``opened_at`` is recorded and the breaker
    blocks every probe for ``CIRCUIT_OPEN_S`` seconds. On the next
    probe after that, the breaker closes and the error window is
    cleared.
    """

    _errors: deque[float] = field(default_factory=deque)
    opened_at: float | None = None

    def is_open(self, now: float | None = None) -> bool:
        """``True`` while the breaker is open. Probes short-circuit
        to ``None`` without touching Redis."""
        if self.opened_at is None:
            return False
        current = now if now is not None else time.monotonic()
        if current - self.opened_at >= CIRCUIT_OPEN_S:
            # Open period elapsed — close and let the next probe try.
            self.opened_at = None
            self._errors.clear()
            return False
        return True

    def record_error(self, now: float | None = None) -> None:
        current = now if now is not None else time.monotonic()
        # Drop entries older than the sliding window.
        cutoff = current - CIRCUIT_WINDOW_S
        while self._errors and self._errors[0] < cutoff:
            self._errors.popleft()
        self._errors.append(current)
        if len(self._errors) >= CIRCUIT_ERROR_THRESHOLD and self.opened_at is None:
            self.opened_at = current

    def record_success(self) -> None:
        # A single success after a non-open run resets the window.
        # We deliberately do NOT reset while OPEN — the open period
        # must elapse for the breaker to close.
        if self.opened_at is None:
            self._errors.clear()


# ── Per-page call budget ──────────────────────────────────────────


@dataclass
class RedisCallBudget:
    """Per-page Redis call budget. Page handlers enter the context
    manager with a fresh budget; every probe consumes one call.
    Exceeding the cap returns ``None`` — the dashboard does not
    pretend a missing probe succeeded."""

    limit: int
    consumed: int = 0

    def consume(self, n: int = 1) -> bool:
        if self.consumed + n > self.limit:
            return False
        self.consumed += n
        return True

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.consumed)


@contextmanager
def open_budget(*, limit: int) -> Iterator[RedisCallBudget]:
    """Open a fresh per-page budget. Pages do::

    with open_budget(limit=1 + len(STREAMS)) as budget:
        await xlen(redis, stream, breaker=br, budget=budget)
        ...
    """
    yield RedisCallBudget(limit=limit)


# ── Probe wrappers ────────────────────────────────────────────────


async def _run[T](
    factory: Callable[[], Awaitable[T]],
    *,
    breaker: RedisCircuitBreaker,
    budget: RedisCallBudget,
) -> T | None:
    """Run ``factory()`` under the breaker + budget + timeout discipline.

    Takes a factory callable rather than a pre-built awaitable so that
    when the breaker is open or the budget is exhausted, the coroutine
    is NEVER created — avoiding the "coroutine was never awaited"
    warning that fires when a Coroutine object is GC'd unstarted.
    """
    if breaker.is_open():
        return None
    if not budget.consume():
        return None
    try:
        result = await asyncio.wait_for(factory(), timeout=PROBE_TIMEOUT_S)
    except TimeoutError:
        breaker.record_error()
        return None
    except Exception:
        breaker.record_error()
        return None
    breaker.record_success()
    return result


async def xlen(
    redis: _RedisLike,
    stream: str,
    *,
    breaker: RedisCircuitBreaker,
    budget: RedisCallBudget,
) -> int | None:
    """``XLEN stream`` — O(1). Returns ``None`` on probe failure."""
    return await _run(lambda: redis.xlen(stream), breaker=breaker, budget=budget)


async def xpending_summary(
    redis: _RedisLike,
    stream: str,
    group: str,
    *,
    breaker: RedisCircuitBreaker,
    budget: RedisCallBudget,
) -> int | None:
    """``XPENDING stream group`` summary — O(1). Returns the pending
    count, or ``None`` on probe failure. We deliberately do NOT
    issue the per-message form (``XPENDING ... <start> <end> <count>``)
    even when called with a ``count`` arg in redis-py — that form is
    O(N) over pending entries."""
    raw = await _run(
        lambda: redis.xpending(stream, group),
        breaker=breaker,
        budget=budget,
    )
    if raw is None:
        return None
    # redis-py returns a dict like {"pending": int, ...} for the summary form.
    if isinstance(raw, dict):
        pending = raw.get("pending")
        return int(pending) if pending is not None else None
    # Older redis-py / raw-protocol shape: tuple (count, min_id, max_id, [...]).
    if isinstance(raw, (tuple, list)) and raw:
        return int(raw[0])
    return None


async def xinfo_stream_lite(
    redis: _RedisLike,
    stream: str,
    *,
    breaker: RedisCircuitBreaker,
    budget: RedisCallBudget,
) -> dict[str, Any] | None:
    """``XINFO STREAM stream`` — O(1). Returns a dict with the
    length and last-entry id only; the full ``XINFO STREAM ... FULL``
    form is forbidden because it walks consumers / groups."""
    raw = await _run(
        lambda: redis.xinfo_stream(stream),
        breaker=breaker,
        budget=budget,
    )
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None
    last_entry = raw.get("last-entry") or raw.get("last_entry")
    last_entry_id: str | None = None
    if isinstance(last_entry, (tuple, list)) and last_entry:
        first = last_entry[0]
        if isinstance(first, (bytes, str)):
            last_entry_id = first.decode() if isinstance(first, bytes) else first
    return {
        "length": int(raw.get("length", 0)),
        "last_entry_id": last_entry_id,
    }


async def scard(
    redis: _RedisLike,
    key: str,
    *,
    breaker: RedisCircuitBreaker,
    budget: RedisCallBudget,
) -> int | None:
    """``SCARD key`` — O(1). Returns the set cardinality, or
    ``None`` on probe failure."""
    return await _run(lambda: redis.scard(key), breaker=breaker, budget=budget)


__all__ = [
    "CIRCUIT_ERROR_THRESHOLD",
    "CIRCUIT_OPEN_S",
    "CIRCUIT_WINDOW_S",
    "PROBE_TIMEOUT_S",
    "RedisCallBudget",
    "RedisCircuitBreaker",
    "open_budget",
    "scard",
    "xinfo_stream_lite",
    "xlen",
    "xpending_summary",
]
