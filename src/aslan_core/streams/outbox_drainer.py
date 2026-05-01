"""Outbox → Redis drainer daemon.

Codex spec §6 — long-running daemon that drains pending
``streams.outbox`` rows to their Redis stream via ``XADD``. Multiple
drainer processes against the same Postgres are safe because the
``SELECT … FOR UPDATE SKIP LOCKED`` clause guarantees each pending row
is visible to at most one worker per iteration (codex critical-contract
item 5).

The contract for one batch:

  1. ``SELECT`` up to ``batch_size`` outbox rows ``WHERE published_at
     IS NULL ORDER BY created_at FOR UPDATE SKIP LOCKED``.
  2. For each row:
        a. ``XADD`` to the named Redis stream with bounded ``MAXLEN ~``.
        b. ``UPDATE streams.outbox SET published_at = now(),
           redis_message_id = :rid`` AND ``INSERT INTO
           streams.event_id_to_redis(event_id, stream_name,
           redis_message_id)`` IN THE SAME TRANSACTION (codex
           critical-contract item 6).
        c. On any per-row exception (XADD timeout, network error,
           etc.), bump ``publish_attempts`` + record ``last_error``;
           the row stays pending and is retried on the next iteration.
  3. Emit ONE ``stream.outbox_drained`` audit row per non-empty batch
     with bounded forensic metadata (drained_count, oldest_age_s,
     newest_age_s, any_failure_count).
  4. Update the ``aslan_stream_outbox_pending`` Prometheus gauge from a
     fresh ``COUNT(*) WHERE published_at IS NULL`` so dashboards stay
     live without scraping the DB on every interval.
  5. ``await asyncio.sleep(poll_interval_s)``; if ``once=True`` exit
     after one iteration (used by ``aslan streams drain --once`` and
     by tests).

The drainer auto-stamps a system actor (``system:streams.outbox_drainer``)
at the start of every call so audit emission always passes the
strict-mode check. The previous ContextVar value is saved + restored
in a try/finally so the call doesn't leak actor state to siblings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.audit import (
    Actor,
    AuditRecord,
    current_actor,
    set_actor,
)
from aslan_core.audit import record as audit_record
from aslan_core.observability.metrics import (
    aslan_stream_outbox_drain_duration_seconds,
    aslan_stream_outbox_pending,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

_log = logging.getLogger(__name__)


_DRAINER_ACTOR: Actor = Actor(
    actor_id="system:streams.outbox_drainer",
    actor_kind="system",
)


async def drain_outbox(
    *,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    poll_interval_s: float = 1.0,
    batch_size: int = 100,
    max_attempts_before_alert: int = 5,
    redis_default_maxlen: int = 100_000,
    once: bool = False,
) -> None:
    """Long-running outbox-drainer daemon (or one-shot if ``once=True``).

    :param redis: Async Redis client (``redis.asyncio.Redis``).
    :param session_factory: AsyncSession factory; the drainer opens a
        fresh session per batch so a transient DB error in one batch
        does not poison the next.
    :param poll_interval_s: Seconds to sleep between batches when the
        outbox is empty (and between non-empty batches in long-running
        mode). Defaults to 1.0.
    :param batch_size: Max rows fetched per ``SELECT … FOR UPDATE SKIP
        LOCKED``. Defaults to 100.
    :param max_attempts_before_alert: After this many publish_attempts
        the drainer emits a structured WARNING for the row. The row
        is NOT auto-dropped; an operator must intervene.
    :param redis_default_maxlen: ``MAXLEN ~`` cap passed to ``XADD``.
        Bounds the stream length so a slow consumer doesn't run Redis
        out of memory.
    :param once: If True, return after a single iteration. Used by the
        ``aslan streams drain --once`` CLI subcommand and by tests.
    """
    prev_actor = current_actor()
    set_actor(_DRAINER_ACTOR)
    try:
        while True:
            t0 = time.perf_counter()
            async with session_factory() as session:
                drained, oldest_age_s, newest_age_s, failures = await _drain_one_batch(
                    session=session,
                    redis=redis,
                    batch_size=batch_size,
                    redis_default_maxlen=redis_default_maxlen,
                    max_attempts_before_alert=max_attempts_before_alert,
                )
                if drained > 0:
                    await audit_record(
                        session,
                        record=AuditRecord(
                            operation="stream.outbox_drained",
                            target_schema="streams",
                            target_table="outbox",
                            target_pk={"batch_drained_count": drained},
                            before=None,
                            after=None,
                            metadata={
                                "drained_count": drained,
                                "oldest_age_s": oldest_age_s,
                                "newest_age_s": newest_age_s,
                                "any_failure_count": failures,
                            },
                        ),
                    )
                await session.commit()

            # Refresh the pending gauge from a fresh COUNT(*) so the
            # value reflects post-batch state regardless of failures
            # inside the batch above.
            async with session_factory() as gs:
                pending: int = (
                    await gs.execute(
                        text("SELECT count(*) FROM streams.outbox WHERE published_at IS NULL")
                    )
                ).scalar_one()
            aslan_stream_outbox_pending.inc(0)  # ensures the lazy handle is materialised
            _set_gauge(aslan_stream_outbox_pending, float(pending))

            elapsed = time.perf_counter() - t0
            aslan_stream_outbox_drain_duration_seconds.observe(elapsed)

            if once:
                return
            await asyncio.sleep(poll_interval_s)
    finally:
        set_actor(prev_actor)


def _set_gauge(gauge: Any, value: float) -> None:
    """Set a :class:`_LazyGauge` to ``value``. The lazy gauge wrapper
    proxies ``.set(...)`` to the underlying prometheus_client primitive
    when available; otherwise no-ops."""
    impl = gauge._ensure_impl()
    if impl is None:
        return
    impl.set(value)


async def _drain_one_batch(
    *,
    session: AsyncSession,
    redis: Redis,
    batch_size: int,
    redis_default_maxlen: int,
    max_attempts_before_alert: int,
) -> tuple[int, float, float, int]:
    """Process up to ``batch_size`` pending outbox rows in one transaction.

    Returns ``(drained_count, oldest_age_s, newest_age_s,
    failure_count)``. The transaction is left open — the caller commits
    after auditing the batch.
    """
    rows = (
        await session.execute(
            text(
                "SELECT outbox_id, stream_name, event_id, schema_version, "
                "       payload, source_id, created_at "
                "FROM streams.outbox "
                "WHERE published_at IS NULL "
                "ORDER BY created_at "
                "LIMIT :n FOR UPDATE SKIP LOCKED"
            ),
            {"n": batch_size},
        )
    ).all()

    if not rows:
        return 0, 0.0, 0.0, 0

    drained = 0
    failures = 0
    now = datetime.now(UTC)
    oldest = min(r.created_at for r in rows)
    newest = max(r.created_at for r in rows)

    for row in rows:
        # Per-row SAVEPOINT so a per-row exception (XADD timeout,
        # Redis network error, …) doesn't poison the outer transaction
        # — the outer SELECT FOR UPDATE locks are kept and the OTHER
        # pending rows in this batch still complete.
        sp = await session.begin_nested()
        try:
            # redis-py's xadd() type-stub asks for the broad str|bytes|...
            # union for both keys and values; we always pass str → str so
            # build the dict with that broader annotation up-front.
            xadd_fields: dict[
                bytes | bytearray | memoryview | str | int | float,
                bytes | bytearray | memoryview | str | int | float,
            ] = {
                "event_id": str(row.event_id),
                "schema_version": str(row.schema_version),
                "payload": json.dumps(row.payload),
                "traceparent": str(row.payload.get("traceparent") or ""),
                "kind": str(row.payload.get("kind") or ""),
            }
            redis_message_id = await redis.xadd(
                row.stream_name,
                xadd_fields,
                maxlen=redis_default_maxlen,
                approximate=True,
            )
            # Both the outbox UPDATE and the event_id_to_redis INSERT
            # MUST run in the same transaction so a crash between them
            # leaves the outbox row pending — at-least-once semantics
            # plus consumer-side event_id dedup gives exactly-once
            # observable delivery (codex critical-contract item 6 +
            # spec §6 + Task 11 idempotency proof).
            await session.execute(
                text(
                    "UPDATE streams.outbox "
                    "SET published_at = now(), redis_message_id = :rid "
                    "WHERE outbox_id = :oid"
                ),
                {"rid": redis_message_id, "oid": row.outbox_id},
            )
            await session.execute(
                text(
                    "INSERT INTO streams.event_id_to_redis "
                    "(event_id, stream_name, redis_message_id) "
                    "VALUES (:eid, :sn, :rid) "
                    "ON CONFLICT DO NOTHING"
                ),
                {
                    "eid": str(row.event_id),
                    "sn": row.stream_name,
                    "rid": redis_message_id,
                },
            )
            await sp.commit()
            drained += 1
        except Exception:
            failures += 1
            await sp.rollback()
            tb = traceback.format_exc()[:4000]
            # Bump publish_attempts in a fresh SAVEPOINT so the bookkeeping
            # write is durable even though the XADD path failed. The
            # outer transaction still owns the FOR UPDATE locks for the
            # remaining rows in the batch.
            sp_err = await session.begin_nested()
            await session.execute(
                text(
                    "UPDATE streams.outbox "
                    "SET publish_attempts = publish_attempts + 1, "
                    "    last_attempt_at = now(), "
                    "    last_error = :err "
                    "WHERE outbox_id = :oid"
                ),
                {"err": tb, "oid": row.outbox_id},
            )
            await sp_err.commit()
            new_attempts = (
                await session.execute(
                    text("SELECT publish_attempts FROM streams.outbox WHERE outbox_id = :oid"),
                    {"oid": row.outbox_id},
                )
            ).scalar_one()
            if new_attempts > max_attempts_before_alert:
                _log.warning(
                    "outbox row %s has failed %s times; manual intervention",
                    row.outbox_id,
                    new_attempts,
                )

    oldest_age = (now - oldest).total_seconds()
    newest_age = (now - newest).total_seconds()
    return drained, oldest_age, newest_age, failures


__all__ = ["drain_outbox"]
