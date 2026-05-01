"""Integration tests for ``StreamConsumer.consume`` (v0.5.0 Task 12).

Happy paths only — XREADGROUP loop + parse + yield + caller-driven ACK.
Tasks 13-14's split claim/processed gate and dead-letter routing are
covered by the dedicated test files; the gates ARE active here so
these tests verify the happy-path threading through the full
implementation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.errors import StreamSchemaVersionMismatch
from aslan_core.streams import (
    FilingNewEvent,
    StreamConsumer,
    StreamProducer,
    drain_outbox,
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
                "VALUES('kap','consume_test','succeeded') "
                "RETURNING ingestion_run_id"
            )
        )
    ).scalar_one()
    await session.commit()
    return rid


async def _produce_and_drain(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    n: int = 1,
    schema_version: int = 1,
) -> int:
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    for _ in range(n):
        await producer.publish(_ev(schema_version=schema_version))
    await session.commit()
    await drain_outbox(
        redis=redis_client,
        session_factory=session_factory,
        once=True,
    )
    return rid


@pytest.fixture
async def _flush_redis(redis_client: Redis) -> AsyncIterator[None]:
    await redis_client.flushdb()
    yield
    await redis_client.flushdb()


async def test_consume_yields_parsed_event(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    await _produce_and_drain(session, redis_client, session_factory)

    consumer = StreamConsumer(
        redis=redis_client,
        session_factory=session_factory,
        max_supported_schema_version=1,
        min_supported_schema_version=1,
    )
    await consumer.ensure_group("aslan.kap.filings.new", "internal-test")

    received: list[FilingNewEvent] = []
    async for event, ack in consumer.consume(
        "aslan.kap.filings.new",
        "internal-test",
        "consumer-1",
        block_ms=100,
        count=10,
    ):
        assert isinstance(event, FilingNewEvent)
        received.append(event)
        await ack()
        break  # one event then break out
    assert len(received) == 1
    assert received[0].kind == "filing.new"


async def test_consume_caller_success_acks_message(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    await _produce_and_drain(session, redis_client, session_factory)
    consumer = StreamConsumer(
        redis=redis_client,
        session_factory=session_factory,
        max_supported_schema_version=1,
    )
    await consumer.ensure_group("aslan.kap.filings.new", "internal-test")

    async for _event, ack in consumer.consume(
        "aslan.kap.filings.new",
        "internal-test",
        "c1",
        block_ms=100,
    ):
        await ack()
        break
    pending = await redis_client.xpending("aslan.kap.filings.new", "internal-test")
    assert pending["pending"] == 0


async def test_consume_caller_exception_does_not_ack(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    """Caller exception → message remains in PEL, no ACK."""
    await _produce_and_drain(session, redis_client, session_factory)
    consumer = StreamConsumer(
        redis=redis_client,
        session_factory=session_factory,
        max_supported_schema_version=1,
    )
    await consumer.ensure_group("aslan.kap.filings.new", "internal-test")

    with pytest.raises(RuntimeError, match="caller boom"):
        async for _event, _ack in consumer.consume(
            "aslan.kap.filings.new",
            "internal-test",
            "c1",
            block_ms=100,
        ):
            raise RuntimeError("caller boom")

    pending = await redis_client.xpending("aslan.kap.filings.new", "internal-test")
    assert pending["pending"] >= 1


async def test_consume_two_consumers_same_group_split_messages(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    """Multiple consumers in same group → each entry delivered to ONE."""
    await _produce_and_drain(session, redis_client, session_factory, n=4)
    consumer = StreamConsumer(
        redis=redis_client,
        session_factory=session_factory,
        max_supported_schema_version=1,
    )
    await consumer.ensure_group("aslan.kap.filings.new", "internal-test")

    seen_a: list[str] = []
    seen_b: list[str] = []

    async def _drain(name: str, sink: list[str]) -> None:
        async for event, ack in consumer.consume(
            "aslan.kap.filings.new",
            "internal-test",
            name,
            block_ms=200,
            count=2,
        ):
            sink.append(str(event.event_id))
            await ack()
            if len(sink) >= 2:
                return

    await asyncio.gather(_drain("a", seen_a), _drain("b", seen_b))

    # No overlap; total of 4.
    assert len(set(seen_a) & set(seen_b)) == 0
    assert len(seen_a) + len(seen_b) == 4


async def test_consume_two_groups_each_see_all_messages(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    """Two consumer groups → each sees ALL entries (fan-out)."""
    await _produce_and_drain(session, redis_client, session_factory, n=2)
    consumer = StreamConsumer(
        redis=redis_client,
        session_factory=session_factory,
        max_supported_schema_version=1,
    )
    await consumer.ensure_group("aslan.kap.filings.new", "g4a")
    await consumer.ensure_group("aslan.kap.filings.new", "g4b")

    async def _drain(group: str) -> int:
        n = 0
        async for _event, ack in consumer.consume(
            "aslan.kap.filings.new",
            group,
            f"c-{group}",
            block_ms=200,
            count=5,
        ):
            n += 1
            await ack()
            if n >= 2:
                return n
        return n

    a, b = await asyncio.gather(_drain("g4a"), _drain("g4b"))
    assert a == 2
    assert b == 2


async def test_consume_schema_mismatch_newer_raises(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    """Codex spec §4: received > max_supported → StreamSchemaVersionMismatch
    (direction='newer'). With deadletter_on_schema_mismatch=False, the
    exception bubbles to the caller."""
    await _produce_and_drain(
        session,
        redis_client,
        session_factory,
        schema_version=99,
    )

    consumer = StreamConsumer(
        redis=redis_client,
        session_factory=session_factory,
        max_supported_schema_version=1,
        min_supported_schema_version=1,
        deadletter_on_schema_mismatch=False,
    )
    await consumer.ensure_group("aslan.kap.filings.new", "internal-test")

    with pytest.raises(StreamSchemaVersionMismatch) as exc_info:
        async for _event, ack in consumer.consume(
            "aslan.kap.filings.new",
            "internal-test",
            "c1",
            block_ms=200,
        ):
            await ack()
            break
    assert exc_info.value.direction == "newer"
    assert exc_info.value.received_version == 99


async def test_consume_schema_mismatch_older_raises(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    """Codex spec §4: received < min_supported → direction='older'."""
    await _produce_and_drain(session, redis_client, session_factory, schema_version=1)

    consumer = StreamConsumer(
        redis=redis_client,
        session_factory=session_factory,
        max_supported_schema_version=5,
        min_supported_schema_version=2,
        deadletter_on_schema_mismatch=False,
    )
    await consumer.ensure_group("aslan.kap.filings.new", "internal-test")

    with pytest.raises(StreamSchemaVersionMismatch) as exc_info:
        async for _event, ack in consumer.consume(
            "aslan.kap.filings.new",
            "internal-test",
            "c1",
            block_ms=200,
        ):
            await ack()
            break
    assert exc_info.value.direction == "older"


async def test_consume_increments_consumes_counter(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    pytest.importorskip("prometheus_client")
    await _produce_and_drain(session, redis_client, session_factory)
    from aslan_core.observability.metrics import aslan_stream_consumes_total

    before = aslan_stream_consumes_total.labels(
        stream="aslan.kap.filings.new",
        group="internal-test",
    )._value.get()

    consumer = StreamConsumer(
        redis=redis_client,
        session_factory=session_factory,
        max_supported_schema_version=1,
    )
    await consumer.ensure_group("aslan.kap.filings.new", "internal-test")
    async for _event, ack in consumer.consume(
        "aslan.kap.filings.new",
        "internal-test",
        "c1",
        block_ms=200,
    ):
        await ack()
        break

    after = aslan_stream_consumes_total.labels(
        stream="aslan.kap.filings.new",
        group="internal-test",
    )._value.get()
    assert after >= before + 1
