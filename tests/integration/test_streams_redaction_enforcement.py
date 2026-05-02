"""GDPR Art. 17 stream redaction enforcement tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.errors import StreamRedactionIntegrityError
from aslan_core.streams import (
    FilingNewEvent,
    StreamConsumer,
    StreamProducer,
    canonical_payload_hash,
    drain_outbox,
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
        "title": "Jane Doe material event",
        "published_at": datetime.now(UTC),
        "primary_object_key": "k",
        "bucket": "b",
        "is_revision": False,
        "revision_no": 1,
    }
    base.update(override)
    return FilingNewEvent(**base)


async def test_consumer_yields_registry_payload_when_event_redacted(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event = await _publish_and_drain(session, redis_client, session_factory)
    redacted_payload = event.model_dump(mode="json")
    redacted_payload["title"] = "<redacted>"
    await write_registry_entry(
        session,
        event_id=event.event_id,
        redaction_reason="Art.17",
        original_stream="aslan.kap.filings.new",
        redacted_payload=redacted_payload,
        redacted_payload_hash=canonical_payload_hash(redacted_payload),
        original_payload_hash=canonical_payload_hash(event.model_dump(mode="json")),
    )
    await session.commit()

    consumer = StreamConsumer(
        redis=redis_client,
        session_factory=session_factory,
        max_supported_schema_version=1,
    )
    await consumer.ensure_group("aslan.kap.filings.new", "internal-test")

    async for consumed, ack in consumer.consume(
        "aslan.kap.filings.new",
        "internal-test",
        "redaction-consumer",
        block_ms=100,
    ):
        assert isinstance(consumed, FilingNewEvent)
        assert consumed.event_id == event.event_id
        assert consumed.title == "<redacted>"
        await ack()
        break

    assert await _audit_count(session, "stream.consumed_redacted", event.event_id) == 1


async def test_consumer_halts_on_redaction_payload_hash_mismatch(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event = await _publish_and_drain(session, redis_client, session_factory)
    redacted_payload = event.model_dump(mode="json")
    redacted_payload["title"] = "<redacted>"
    await write_registry_entry(
        session,
        event_id=event.event_id,
        redaction_reason="Art.17",
        original_stream="aslan.kap.filings.new",
        redacted_payload=redacted_payload,
        redacted_payload_hash="0" * 64,
        original_payload_hash=canonical_payload_hash(event.model_dump(mode="json")),
    )
    await session.commit()

    consumer = StreamConsumer(
        redis=redis_client,
        session_factory=session_factory,
        max_supported_schema_version=1,
    )
    await consumer.ensure_group("aslan.kap.filings.new", "internal-test")

    with pytest.raises(StreamRedactionIntegrityError):
        async for _event, _ack in consumer.consume(
            "aslan.kap.filings.new",
            "internal-test",
            "redaction-consumer",
            block_ms=100,
        ):
            raise AssertionError("tampered redaction payload must not yield")
    assert not await redis_client.exists(
        f"stream:aslan.kap.filings.new:internal-test:claim:{event.event_id}"
    )


async def _publish_and_drain(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> FilingNewEvent:
    await session.execute(text("DELETE FROM streams.redaction_registry"))
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
                "VALUES('kap','redaction_test','succeeded') "
                "RETURNING ingestion_run_id"
            )
        )
    ).scalar_one()
    event = _ev(producer_run_id=rid)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    await producer.publish(event)
    await session.commit()
    await drain_outbox(redis=redis_client, session_factory=session_factory, once=True)
    return event


async def _audit_count(
    session: AsyncSession,
    operation: str,
    event_id: UUID,
) -> int:
    return int(
        (
            await session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM audit.events
                    WHERE operation = :operation
                      AND target_pk->>'event_id' = :event_id
                    """
                ),
                {"operation": operation, "event_id": str(event_id)},
            )
        ).scalar_one()
    )
