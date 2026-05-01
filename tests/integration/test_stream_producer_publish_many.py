"""Integration tests for ``StreamProducer.publish_many`` (Task 9 of v0.5.0).

Covers Phase 0 in-memory dedup (codex F-impl test pattern + critical-
contract item 1), ONE-audit-per-call (item 4), event_ids[] truncation
to 100, empty-list no-op, and cross-batch UNIQUE conflict propagation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.errors import StreamEventIdConflict
from aslan_core.models.streams import Outbox
from aslan_core.streams import FilingNewEvent, StreamProducer

pytestmark = pytest.mark.integration


async def _seed_run(session: AsyncSession) -> int:
    # Wipe stream-related tables so each test asserts on its own writes.
    # The Postgres testcontainer is session-scoped and other tests commit
    # outbox + audit rows that would otherwise leak in.
    await session.execute(text("DELETE FROM streams.event_id_to_redis"))
    await session.execute(text("DELETE FROM streams.outbox"))
    await session.execute(text("DELETE FROM audit.events WHERE operation='stream.publish'"))
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
                "VALUES('kap','stream_producer_many_test','succeeded') "
                "RETURNING ingestion_run_id"
            )
        )
    ).scalar_one()
    await session.commit()
    return rid


def _make_event(eid: UUID | None = None) -> FilingNewEvent:
    return FilingNewEvent(
        schema_version=1,
        event_id=eid or uuid4(),
        produced_at=datetime.now(UTC),
        producer_run_id=0,
        source_id="kap",
        filing_id=uuid4(),
        entity_id=None,
        filing_kind="material_event",
        title="t",
        published_at=datetime.now(UTC),
        primary_object_key="k",
        bucket="b",
        is_revision=False,
        revision_no=1,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_many_writes_n_outbox_rows(
    session: AsyncSession,
) -> None:
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    events = [_make_event() for _ in range(5)]
    outbox_ids = await producer.publish_many(events)
    await session.commit()
    assert len(outbox_ids) == 5
    rows = (
        (await session.execute(select(Outbox).where(Outbox.outbox_id.in_(outbox_ids))))
        .scalars()
        .all()
    )
    assert len(rows) == 5


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_many_emits_one_audit_event_per_call(
    session: AsyncSession,
) -> None:
    """Codex critical-contract item 4: ONE audit event per call with
    metadata.event_count + metadata.event_ids[] (first 100).
    """
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    events = [_make_event() for _ in range(3)]
    await producer.publish_many(events)
    await session.commit()
    rows = (
        await session.execute(
            text(
                "SELECT operation, metadata FROM audit.events "
                "WHERE operation='stream.publish' "
                "AND (metadata->>'event_count') IS NOT NULL"
            )
        )
    ).all()
    assert len(rows) == 1
    op, meta = rows[0]
    assert op == "stream.publish"
    assert int(meta["event_count"]) == 3
    assert len(meta["event_ids"]) == 3
    assert meta["streams"] == ["aslan.kap.filings.new"]


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_many_truncates_event_ids_to_100(
    session: AsyncSession,
) -> None:
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    events = [_make_event() for _ in range(101)]
    outbox_ids = await producer.publish_many(events)
    await session.commit()
    assert len(outbox_ids) == 101
    rows = (
        await session.execute(
            text(
                "SELECT metadata FROM audit.events "
                "WHERE operation='stream.publish' "
                "AND (metadata->>'event_count')='101'"
            )
        )
    ).all()
    assert len(rows) == 1
    meta = rows[0][0]
    assert int(meta["event_count"]) == 101
    # truncation cap to 100
    assert len(meta["event_ids"]) == 100


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_many_intra_batch_duplicate_raises_before_db_io(
    session: AsyncSession,
) -> None:
    """Phase 0: same event_id twice in the input → raise BEFORE any DB
    I/O. Spy assertion on session.execute / session.flush.
    """
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    eid = uuid4()
    events = [_make_event(eid=eid), _make_event(eid=eid)]

    with (
        patch.object(session, "execute", new_callable=AsyncMock) as exec_spy,
        patch.object(session, "flush", new_callable=AsyncMock) as flush_spy,
        pytest.raises(StreamEventIdConflict),
    ):
        await producer.publish_many(events)
    exec_spy.assert_not_called()
    flush_spy.assert_not_called()


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_many_cross_batch_duplicate_rolls_back_full_batch(
    session: AsyncSession,
) -> None:
    """Same event_id as a row that already exists in outbox → DB UNIQUE
    conflict on the per-row flush → caller's transaction rolls back, no
    partial writes survive after rollback.
    """
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    eid = uuid4()
    await producer.publish(_make_event(eid=eid))
    await session.commit()
    # Confirm exactly one row exists pre-batch.
    pre = (await session.execute(text("SELECT count(*) FROM streams.outbox"))).scalar_one()
    assert pre == 1

    new_events = [_make_event() for _ in range(3)] + [_make_event(eid=eid)]
    with pytest.raises(StreamEventIdConflict):
        await producer.publish_many(new_events)
    # The producer rolled back via _insert_outbox's IntegrityError handler;
    # any rows it had flushed before the conflict are gone.
    post = (await session.execute(text("SELECT count(*) FROM streams.outbox"))).scalar_one()
    assert post == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_many_empty_list_is_noop(session: AsyncSession) -> None:
    """An empty list emits no audit row and writes no outbox rows."""
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    pre_count = (await session.execute(text("SELECT count(*) FROM streams.outbox"))).scalar_one()
    pre_audit = (await session.execute(text("SELECT count(*) FROM audit.events"))).scalar_one()
    out = await producer.publish_many([])
    await session.commit()
    assert out == []
    post_count = (await session.execute(text("SELECT count(*) FROM streams.outbox"))).scalar_one()
    post_audit = (await session.execute(text("SELECT count(*) FROM audit.events"))).scalar_one()
    assert post_count == pre_count
    assert post_audit == pre_audit


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_many_increments_prometheus_per_event(
    session: AsyncSession,
) -> None:
    pytest.importorskip("prometheus_client")
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    from aslan_core.observability.metrics import aslan_stream_publishes_total

    impl = aslan_stream_publishes_total._ensure_impl()
    assert impl is not None
    before = impl.labels(stream="aslan.kap.filings.new", source_id="kap")._value.get()
    await producer.publish_many([_make_event() for _ in range(7)])
    await session.commit()
    after = impl.labels(stream="aslan.kap.filings.new", source_id="kap")._value.get()
    assert after == before + 7


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_many_explicit_stream_kwarg_applies_to_all_events(
    session: AsyncSession,
) -> None:
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    outbox_ids = await producer.publish_many(
        [_make_event() for _ in range(3)],
        stream="aslan.kap.filings.financial_report",
    )
    await session.commit()
    rows = (
        (await session.execute(select(Outbox).where(Outbox.outbox_id.in_(outbox_ids))))
        .scalars()
        .all()
    )
    assert len(rows) == 3
    assert all(r.stream_name == "aslan.kap.filings.financial_report" for r in rows)
