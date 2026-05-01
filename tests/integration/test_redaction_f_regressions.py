"""Codex F13 + F15 redaction-enforcement regressions.

* **F13** — the per-event advisory lock acquired by the consumer
  before yield blocks a concurrent ``streams.redaction_registry``
  write that would otherwise race the authoritative-redaction
  re-check at step 4.
* **F15** — direct INSERT into ``streams.redaction_registry`` is
  REVOKEd from the ``aslan_app`` runtime role; the only mutation
  surface is the ``streams.redaction_registry_insert`` SECURITY
  DEFINER function.

The migration-level F15 test in
``test_migration_0018_streams_redaction_registry.py`` covers the
GRANT/REVOKE matrix from a raw asyncpg connection. This file
re-asserts the same boundary from inside the same-loop session
factory + via :func:`write_registry_entry` (the public API), so a
future contract-change (e.g. SQLAlchemy ORM mapping that bypasses
the function) immediately fails this regression.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.streams import (
    FilingNewEvent,
    StreamConsumer,
    StreamProducer,
    canonical_payload_hash,
    drain_outbox,
    redaction_lock_key,
    write_registry_entry,
)

pytestmark = pytest.mark.integration


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
        "title": "redaction-regression event",
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
                "VALUES('kap','redaction_f_regression','succeeded') "
                "RETURNING ingestion_run_id"
            )
        )
    ).scalar_one()
    await session.commit()
    return rid


# ── F13 — advisory lock blocks concurrent redaction during yield ────


async def test_consumer_holds_advisory_lock_during_yield_blocks_concurrent_redaction(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    pg_dsn: str,
) -> None:
    """F13 — while the consumer is mid-yield, it holds the per-event
    advisory lock (``pg_advisory_xact_lock``). A concurrent transaction
    that tries to acquire the SAME lock (which is what
    ``streams.redaction_registry_insert`` does first) MUST block until
    the consumer commits its session.

    We use ``pg_try_advisory_xact_lock`` for the probe so we can
    distinguish "currently held" from "available" without flake-prone
    timeouts. The probe is run on a sibling connection while the
    consumer is paused inside the ``async for`` body."""
    import asyncpg

    await redis_client.flushdb()
    rid = await _seed_run(session)
    stream = "aslan.kap.filings.new"
    group = "internal-test"
    event = _ev(producer_run_id=rid)

    producer = StreamProducer(session=session, ingestion_run_id=rid)
    await producer.publish(event)
    await session.commit()
    await drain_outbox(redis=redis_client, session_factory=session_factory, once=True)

    consumer = StreamConsumer(
        redis=redis_client,
        session_factory=session_factory,
        max_supported_schema_version=1,
    )
    await consumer.ensure_group(stream, group)

    raw_dsn = pg_dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    probe = await asyncpg.connect(dsn=raw_dsn)
    try:
        async for ev_consumed, ack in consumer.consume(
            stream,
            group,
            "c-lock",
            block_ms=200,
        ):
            # The consumer holds the advisory lock under its session
            # right now. Probe from a SECOND, sibling connection.
            await probe.execute("BEGIN")
            held: bool = not await probe.fetchval(
                "SELECT pg_try_advisory_xact_lock(hashtextextended($1, 0))",
                redaction_lock_key(ev_consumed.event_id),
            )
            await probe.execute("ROLLBACK")
            assert held, (
                "consumer must hold the per-event advisory lock during yield; "
                "probe acquired the lock concurrently"
            )
            await ack()

            # After ack(), the consumer commits + closes the lock
            # session. The lock is now released; a fresh probe should
            # succeed.
            await probe.execute("BEGIN")
            now_free: bool = await probe.fetchval(
                "SELECT pg_try_advisory_xact_lock(hashtextextended($1, 0))",
                redaction_lock_key(ev_consumed.event_id),
            )
            await probe.execute("ROLLBACK")
            assert now_free, "lock must be released once consumer commits"
            break
    finally:
        await probe.close()
        await redis_client.flushdb()


# ── F15 — direct INSERT into redaction_registry rejected for app role ──


async def test_direct_insert_to_redaction_registry_rejected_for_app_role(
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    pg_dsn: str,
) -> None:
    """F15 — connect AS the ``aslan_app`` runtime role; attempt direct
    INSERT into ``streams.redaction_registry``; assert it raises
    ``InsufficientPrivilegeError``. Then call the SECURITY DEFINER
    function ``streams.redaction_registry_insert(...)`` and assert
    success.

    Complements the migration-level F15 tests in
    ``test_migration_0018_streams_redaction_registry.py`` by also
    exercising the public-API path
    (:func:`aslan_core.streams.write_registry_entry`) so a future
    contract change that bypasses the function (e.g. an ORM mapping
    leak) breaks this regression."""
    import asyncpg

    raw_dsn = pg_dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn=raw_dsn)
    try:
        await conn.execute("SET ROLE aslan_app")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO streams.redaction_registry "
                "(event_id, redaction_reason, original_stream, "
                " redacted_payload, redacted_payload_hash, "
                " original_payload_hash) "
                "VALUES (gen_random_uuid(), 'Art.17', 'aslan.kap.filings.new', "
                "        '{}'::jsonb, repeat('a', 64), repeat('b', 64))"
            )
        # SECURITY DEFINER function path succeeds for the same role.
        await conn.execute(
            "SELECT streams.redaction_registry_insert("
            "  gen_random_uuid(), 'Art.17', now(), 'aslan.kap.filings.new', "
            "  '{}'::jsonb, repeat('a', 64), repeat('b', 64))"
        )
    finally:
        await conn.execute("RESET ROLE")
        await conn.close()

    # Public surface (write_registry_entry) round-trip via the
    # framework, not the raw SQL.
    eid = uuid4()
    payload = {"x": 1}
    async with session_factory() as s:
        await write_registry_entry(
            s,
            event_id=eid,
            redaction_reason="Art.17",
            original_stream="aslan.kap.filings.new",
            redacted_payload=payload,
            redacted_payload_hash=canonical_payload_hash(payload),
            original_payload_hash=canonical_payload_hash({"x": 0}),
            redis=redis_client,
        )
        await s.commit()
    async with session_factory() as s:
        seen = await s.scalar(
            text("SELECT redaction_reason FROM streams.redaction_registry WHERE event_id = :eid"),
            {"eid": str(eid)},
        )
    assert seen == "Art.17"

    # Cleanup so subsequent tests don't see the row.
    async with session_factory() as s:
        await s.execute(
            text("DELETE FROM streams.redaction_registry WHERE event_id = :eid"),
            {"eid": str(eid)},
        )
        await s.commit()
