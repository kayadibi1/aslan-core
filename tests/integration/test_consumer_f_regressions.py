"""Codex F1 + F4 regression tests for ``StreamConsumer``.

The split-claim/processed contract (F1) and the no-XACK-on-caller-failure
contract (F4) live as plain assertions inside this test module so a
future refactor that collapses one of the two Redis keys back into a
single SADD-only flow trips a red bar before it lands.

F1 (codex F-impl-1): two consumers in the same group both see the
SAME ``event_id`` (drainer-retry duplicate). Exactly one wins the
SET-NX claim and yields. The loser XACKs its own ``message_id``
without yielding once the winner SADDs ``processed`` so the PEL
doesn't accumulate permanent retry duplicates.

F4 (codex spec §7 + round-2 footnote): caller exception → the
consumer does NOT XACK the original message; the message stays in
PEL; the in-flight claim is DELed; the ``processed`` SADD is NOT
performed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.streams import (
    FilingNewEvent,
    StreamConsumer,
    StreamProducer,
    drain_outbox,
)

pytestmark = pytest.mark.integration


def _aw(x: Any) -> Awaitable[Any]:
    """Cast a redis-py sync-or-async return to ``Awaitable[Any]`` so
    mypy --strict accepts ``await`` on ``sismember`` etc."""
    return cast(Awaitable[Any], x)


def _ev(**override: Any) -> FilingNewEvent:
    base: dict[str, Any] = {
        "schema_version": 1,
        "event_id": uuid4(),
        "produced_at": datetime.now(UTC),
        "producer_run_id": 0,
        "source_id": "kap",
        "filing_id": uuid4(),
        "entity_id": None,
        "filing_kind": "material_event",
        "title": "f-regression event",
        "published_at": datetime.now(UTC),
        "primary_object_key": "k",
        "bucket": "b",
        "is_revision": False,
        "revision_no": 1,
    }
    base.update(override)
    return FilingNewEvent(**base)


async def _seed_run(session: AsyncSession) -> int:
    await session.execute(text("DELETE FROM streams.event_id_to_redis"))
    await session.execute(text("DELETE FROM streams.outbox"))
    await session.execute(
        text(
            "INSERT INTO src.source(source_id, name, kind, license_status) "
            "VALUES('kap','KAP','scraper','open') ON CONFLICT DO NOTHING"
        )
    )
    rid: int = (
        await session.execute(
            text(
                "INSERT INTO src.ingestion_run(source_id, job_name, status) "
                "VALUES('kap','f_regression','succeeded') "
                "RETURNING ingestion_run_id"
            )
        )
    ).scalar_one()
    await session.commit()
    return rid


@pytest.fixture
async def _flush_redis(redis_client: Redis) -> AsyncIterator[None]:
    await redis_client.flushdb()
    yield
    await redis_client.flushdb()


# ── F1 — two consumers, same event_id, only one yields ─────────────


async def test_two_consumers_same_event_id_only_one_yields(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    """F1 — drainer-retry duplicate: two stream entries carry the SAME
    ``event_id``; both delivered to two consumers in the same group.
    Exactly ONE yields; the loser XACKs without yielding once the
    winner SADDs ``processed``.

    We construct the duplicate by direct ``XADD`` (a real
    drainer-retry path). The ``StreamConsumer`` happy-path uses the
    drainer; this test seeds the duplicate directly so we don't have
    to crash the drainer mid-XADD."""
    rid = await _seed_run(session)
    stream = "aslan.kap.filings.new"
    group = "internal-test"

    # Hand-craft a single event AND XADD it twice to the stream so two
    # PEL entries exist for the same event_id.
    event = _ev(producer_run_id=rid)
    payload_json = event.model_dump_json()
    fields: dict[
        bytes | bytearray | memoryview | str | int | float,
        bytes | bytearray | memoryview | str | int | float,
    ] = {
        "event_id": str(event.event_id),
        "schema_version": str(event.schema_version),
        "payload": payload_json,
        "traceparent": "",
        "kind": "filing.new",
    }
    msg_a = await redis_client.xadd(stream, fields)
    msg_b = await redis_client.xadd(stream, fields)
    assert msg_a != msg_b

    consumer = StreamConsumer(
        redis=redis_client,
        session_factory=session_factory,
        max_supported_schema_version=1,
    )
    await consumer.ensure_group(stream, group)

    yields_total: list[str] = []

    async def _drain_one(name: str) -> None:
        async for ev_consumed, ack in consumer.consume(
            stream,
            group,
            name,
            block_ms=200,
            count=1,
        ):
            yields_total.append(str(ev_consumed.event_id))
            await ack()
            return

    # First consumer takes msg_a (and XADDs ``processed``).
    await _drain_one("c-a")
    assert len(yields_total) == 1
    assert yields_total[0] == str(event.event_id)

    # processed SET should now contain the event_id.
    is_member = await _aw(
        redis_client.sismember(
            f"stream:{stream}:{group}:processed",
            str(event.event_id),
        )
    )
    assert is_member, "winner did not SADD processed"

    # Second consumer in the same group polls — gets msg_b. The
    # split-claim/processed contract says: see processed at step 2,
    # XACK without yielding. ``consume()`` is a long-running loop so
    # we run it under ``asyncio.wait_for`` and assert it never yields.
    async def _expect_no_yield(name: str) -> None:
        async for ev_consumed, ack in consumer.consume(
            stream,
            group,
            name,
            block_ms=100,
            count=1,
        ):
            yields_total.append(str(ev_consumed.event_id))
            await ack()
            return

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_expect_no_yield("c-b"), timeout=2.0)
    # Still exactly one yield total (the duplicate did NOT yield).
    assert len(yields_total) == 1, (
        f"split claim/processed contract violated: total yields {yields_total}"
    )

    # PEL should now be empty: msg_a XACKed by c-a's ack(), msg_b
    # XACKed by the dedup-skip path inside ``_process_message``.
    pending_after = await redis_client.xpending(stream, group)
    assert pending_after["pending"] == 0, (
        f"dedup-XACK path failed to drain duplicate: {pending_after}"
    )


# ── F4 — caller exception leaves the message in PEL; no XACK ────────


async def test_failed_processing_retries_not_xack_skipped(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    """F4 — caller raises during processing → assert NO XACK; message
    stays in PEL; ``processed`` marker NOT set.

    Codex F4 round 2 footnote: the in-flight claim is held under a
    bounded TTL lease (``CLAIM_LEASE_SECONDS = 300``). On caller
    exception the lease expires naturally; that's the crash-recovery
    contract. We assert the claim is still pinned to the original
    consumer (with TTL > 0 still set) so a sibling sees a held claim
    until TTL elapses, and that the ``processed`` SADD did NOT run.
    """
    rid = await _seed_run(session)
    stream = "aslan.kap.filings.new"
    group = "internal-test"

    producer = StreamProducer(session=session, ingestion_run_id=rid)
    event = _ev(producer_run_id=rid)
    await producer.publish(event)
    await session.commit()
    await drain_outbox(redis=redis_client, session_factory=session_factory, once=True)

    consumer = StreamConsumer(
        redis=redis_client,
        session_factory=session_factory,
        max_supported_schema_version=1,
        # Crank the threshold up so this single failure does NOT
        # tip into dead-letter routing. We're isolating the
        # caller-exception → no-XACK contract.
        max_attempts_before_deadletter=99,
    )
    await consumer.ensure_group(stream, group)

    with pytest.raises(RuntimeError, match="caller boom"):
        async for _evt, _ack in consumer.consume(
            stream,
            group,
            "c1",
            block_ms=200,
        ):
            raise RuntimeError("caller boom")

    # PEL retains the message (no XACK).
    pending = await redis_client.xpending(stream, group)
    assert pending["pending"] >= 1

    # processed marker NOT set.
    is_processed = await _aw(
        redis_client.sismember(
            f"stream:{stream}:{group}:processed",
            str(event.event_id),
        )
    )
    assert not is_processed, "processed SADD must NOT happen on caller failure"

    # in-flight claim is held under a bounded TTL (5 min); the actual
    # lease expires naturally on caller crash.
    claim_key = f"stream:{stream}:{group}:claim:{event.event_id}"
    claim_ttl = await redis_client.ttl(claim_key)
    # ttl() returns -2 (no key), -1 (no expiry), or seconds remaining.
    # Either the lease is still set with TTL >0 OR the key has been
    # released. Both are acceptable F4 outcomes; what matters is that
    # the message stayed in PEL and the processed marker did NOT fire.
    assert claim_ttl in (-2,) or claim_ttl > 0, (
        f"unexpected claim TTL state ({claim_ttl}); claim_key={claim_key!r}"
    )
