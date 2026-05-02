"""Integration tests for ``drain_outbox`` (Task 10 of v0.5.0)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.models.streams import EventIdToRedis, Outbox
from aslan_core.streams import FilingNewEvent, StreamProducer, drain_outbox

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
        "title": "t",
        "published_at": datetime.now(UTC),
        "primary_object_key": "k",
        "bucket": "b",
        "is_revision": False,
        "revision_no": 1,
    }
    base.update(override)
    return FilingNewEvent(**base)


async def _seed_run(session: AsyncSession) -> int:
    # Wipe per-test so assertions don't see leftover state from prior tests.
    await session.execute(text("DELETE FROM streams.event_id_to_redis"))
    await session.execute(text("DELETE FROM streams.outbox"))
    await session.execute(text("DELETE FROM audit.events WHERE operation='stream.outbox_drained'"))
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
                "VALUES('kap','outbox_drainer_test','succeeded') "
                "RETURNING ingestion_run_id"
            )
        )
    ).scalar_one()
    await session.commit()
    return rid


@pytest.fixture
async def _flush_redis(redis_client: Redis) -> AsyncIterator[None]:
    """Per-test Redis flush so prior tests' XADDs do not leak in."""
    await redis_client.flushdb()
    yield
    await redis_client.flushdb()


@pytest.mark.asyncio(loop_scope="session")
async def test_drain_outbox_xadds_pending_rows(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    await producer.publish(_ev())
    await session.commit()

    await drain_outbox(
        redis=redis_client,
        session_factory=session_factory,
        once=True,
    )

    # Outbox row must be marked published.
    row = (await session.execute(select(Outbox))).scalar_one()
    assert row.published_at is not None
    assert row.redis_message_id is not None

    # event_id_to_redis row INSERTed in the same tx as the UPDATE.
    eir = (
        await session.execute(select(EventIdToRedis).where(EventIdToRedis.event_id == row.event_id))
    ).scalar_one()
    assert eir.redis_message_id == row.redis_message_id
    assert eir.stream_name == row.stream_name

    # The Redis stream has the entry.
    entries = await redis_client.xrange(row.stream_name, min="-", max="+")
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["event_id"] == str(row.event_id)
    assert int(fields["schema_version"]) == 1
    assert fields["kind"] == "filing.new"


@pytest.mark.asyncio(loop_scope="session")
async def test_drain_outbox_emits_one_audit_per_batch(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    for _ in range(4):
        await producer.publish(_ev())
    await session.commit()

    await drain_outbox(
        redis=redis_client,
        session_factory=session_factory,
        once=True,
        batch_size=10,
    )

    rows = (
        await session.execute(
            text(
                "SELECT operation, metadata FROM audit.events "
                "WHERE operation='stream.outbox_drained'"
            )
        )
    ).all()
    assert len(rows) == 1
    op, meta = rows[0]
    assert op == "stream.outbox_drained"
    assert int(meta["drained_count"]) == 4
    assert int(meta["any_failure_count"]) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_drain_outbox_skip_locked_two_drainers_no_double_publish(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    """Codex spec §6 + critical-contract item 5: two drainers against
    the same DB don't double-publish the same outbox row.
    """
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    for _ in range(10):
        await producer.publish(_ev())
    await session.commit()

    await asyncio.gather(
        drain_outbox(
            redis=redis_client,
            session_factory=session_factory,
            once=True,
            batch_size=10,
        ),
        drain_outbox(
            redis=redis_client,
            session_factory=session_factory,
            once=True,
            batch_size=10,
        ),
    )

    # Each outbox row is published exactly once.
    rows = (await session.execute(select(Outbox))).scalars().all()
    assert len(rows) == 10
    assert all(r.published_at is not None for r in rows)
    assert all(r.redis_message_id is not None for r in rows)

    # No two outbox rows share the same redis_message_id.
    redis_ids = [r.redis_message_id for r in rows]
    assert len(set(redis_ids)) == len(redis_ids)


@pytest.mark.asyncio(loop_scope="session")
async def test_drain_outbox_xadd_failure_increments_publish_attempts(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    """Force an XADD failure (mock the redis client); assert the row's
    publish_attempts is bumped, last_error is populated, and the row
    remains pending (published_at IS NULL).
    """
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    await producer.publish(_ev())
    await session.commit()

    class _FailingRedis:
        async def xadd(self, *_args: Any, **_kw: Any) -> str:
            raise RuntimeError("forced redis failure")

    await drain_outbox(
        redis=_FailingRedis(),  # type: ignore[arg-type]
        session_factory=session_factory,
        once=True,
    )

    row = (await session.execute(select(Outbox))).scalar_one()
    assert row.published_at is None  # still pending
    assert row.publish_attempts == 1
    assert row.last_error is not None
    assert "forced redis failure" in row.last_error


@pytest.mark.asyncio(loop_scope="session")
async def test_drain_outbox_pending_gauge_updated(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    pytest.importorskip("prometheus_client")
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    for _ in range(3):
        await producer.publish(_ev())
    await session.commit()

    from aslan_core.observability.metrics import aslan_stream_outbox_pending

    await drain_outbox(
        redis=redis_client,
        session_factory=session_factory,
        once=True,
    )
    impl = aslan_stream_outbox_pending._ensure_impl()
    assert impl is not None
    assert impl._value.get() == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_drain_outbox_traceparent_field_in_xadd_entry(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    """Codex spec §10: every Redis stream entry carries a traceparent
    field (propagated from event.traceparent).
    """
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    ev = _ev(traceparent="00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01")
    await producer.publish(ev)
    await session.commit()

    await drain_outbox(
        redis=redis_client,
        session_factory=session_factory,
        once=True,
    )
    entries = await redis_client.xrange(
        "aslan.kap.filings.new",
        min="-",
        max="+",
    )
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["traceparent"].startswith("00-aaaa")


@pytest.mark.asyncio(loop_scope="session")
async def test_drain_outbox_empty_outbox_emits_no_audit(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    """No pending rows → drainer no-ops; no audit row, no XADD, gauge=0."""
    await _seed_run(session)
    pre_audit = (
        await session.execute(
            text("SELECT count(*) FROM audit.events WHERE operation='stream.outbox_drained'")
        )
    ).scalar_one()
    await drain_outbox(
        redis=redis_client,
        session_factory=session_factory,
        once=True,
    )
    post_audit = (
        await session.execute(
            text("SELECT count(*) FROM audit.events WHERE operation='stream.outbox_drained'")
        )
    ).scalar_one()
    assert post_audit == pre_audit
