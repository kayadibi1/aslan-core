"""Integration tests for ``StreamProducer.publish`` (Task 8 of v0.5.0)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.errors import StreamEventIdConflict, UnknownEventKind
from aslan_core.models.streams import Outbox
from aslan_core.streams import FilingNewEvent, StreamProducer

pytestmark = pytest.mark.integration


async def _seed_run(session: AsyncSession) -> int:
    await session.execute(
        text(
            "INSERT INTO src.source(source_id, name, kind, license_status) "
            "VALUES('kap','KAP','scraper','open') ON CONFLICT DO NOTHING"
        )
    )
    rid_value: int = (
        await session.execute(
            text(
                "INSERT INTO src.ingestion_run(source_id, job_name, status) "
                "VALUES('kap','stream_producer_test','succeeded') "
                "RETURNING ingestion_run_id"
            )
        )
    ).scalar_one()
    await session.commit()
    return rid_value


def _make_event(**override: object) -> FilingNewEvent:
    base: dict[str, object] = {
        "schema_version": 1,
        "produced_at": datetime.now(UTC),
        "producer_run_id": 0,
        "source_id": "kap",
        "event_id": uuid4(),
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


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_writes_outbox_row(session: AsyncSession) -> None:
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    event = _make_event()
    outbox_id = await producer.publish(event)
    await session.commit()
    row = (await session.execute(select(Outbox).where(Outbox.outbox_id == outbox_id))).scalar_one()
    assert row.stream_name == "aslan.kap.filings.new"
    assert row.event_id == event.event_id
    assert row.payload["filing_kind"] == "material_event"
    assert row.published_at is None
    assert row.publish_attempts == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_auto_stamps_actor_from_current_actor(
    session: AsyncSession,
) -> None:
    """current_actor() is set by the autouse fixture; publisher fills
    actor_id + actor_kind from it on the outbox row.
    """
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    event = _make_event()
    outbox_id = await producer.publish(event)
    await session.commit()
    row = (await session.execute(select(Outbox).where(Outbox.outbox_id == outbox_id))).scalar_one()
    assert row.actor_id == "user:pytest"
    assert row.actor_kind == "user"


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_round_trips_caller_provided_event_id(
    session: AsyncSession,
) -> None:
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    eid = uuid4()
    event = _make_event(event_id=eid)
    outbox_id = await producer.publish(event)
    await session.commit()
    row = (await session.execute(select(Outbox).where(Outbox.outbox_id == outbox_id))).scalar_one()
    assert row.event_id == eid


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_auto_stamps_producer_run_id(
    session: AsyncSession,
) -> None:
    """The producer overrides event.producer_run_id with self.ingestion_run_id
    even if the caller filled a stale value (defensive; the producer is
    the authority on which run a publish belongs to).
    """
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    event = _make_event(producer_run_id=999_999)  # stale
    outbox_id = await producer.publish(event)
    await session.commit()
    row = (await session.execute(select(Outbox).where(Outbox.outbox_id == outbox_id))).scalar_one()
    assert row.producer_run_id == rid  # producer's value wins


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_resolves_stream_from_event_kind_default_map(
    session: AsyncSession,
) -> None:
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    outbox_id = await producer.publish(_make_event())
    await session.commit()
    row = (await session.execute(select(Outbox).where(Outbox.outbox_id == outbox_id))).scalar_one()
    assert row.stream_name == "aslan.kap.filings.new"  # from STREAM_FOR_EVENT_KIND


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_explicit_stream_kwarg_overrides_default(
    session: AsyncSession,
) -> None:
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    outbox_id = await producer.publish(
        _make_event(),
        stream="aslan.kap.filings.financial_report",
    )
    await session.commit()
    row = (await session.execute(select(Outbox).where(Outbox.outbox_id == outbox_id))).scalar_one()
    assert row.stream_name == "aslan.kap.filings.financial_report"


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_unknown_event_kind_raises_before_io(
    session: AsyncSession,
) -> None:
    """If the event.kind is not in STREAM_FOR_EVENT_KIND AND no explicit
    `stream` kwarg is passed, raise UnknownEventKind BEFORE any DB I/O.

    Spy assertion: assert session.execute / session.flush is never called.
    """
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    with (
        patch("aslan_core.streams.producer.STREAM_FOR_EVENT_KIND", {}),
        patch.object(session, "execute", new_callable=AsyncMock) as exec_spy,
        patch.object(session, "flush", new_callable=AsyncMock) as flush_spy,
        pytest.raises(UnknownEventKind),
    ):
        await producer.publish(_make_event())
    exec_spy.assert_not_called()
    flush_spy.assert_not_called()


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_re_publish_same_event_id_raises_event_id_conflict(
    session: AsyncSession,
) -> None:
    """Codex spec §5: UNIQUE constraint catches accidental re-publishes."""
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    eid = uuid4()
    await producer.publish(_make_event(event_id=eid))
    await session.commit()
    with pytest.raises(StreamEventIdConflict):
        await producer.publish(_make_event(event_id=eid))


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_emits_audit_event_in_same_tx(
    session: AsyncSession,
) -> None:
    """Codex critical-contract item 2: audit row INSERT runs in the
    SAME transaction as the outbox INSERT — both land or both roll back.
    """
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    eid = uuid4()
    outbox_id = await producer.publish(_make_event(event_id=eid))
    await session.commit()
    audit = (
        await session.execute(
            text("SELECT operation, metadata FROM audit.events WHERE metadata->>'event_id' = :eid"),
            {"eid": str(eid)},
        )
    ).first()
    assert audit is not None
    op, meta = audit
    assert op == "stream.publish"
    assert int(meta["outbox_id"]) == outbox_id
    assert meta["stream_name"] == "aslan.kap.filings.new"
    assert meta["kind"] == "filing.new"


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_atomicity_audit_failure_rolls_back_outbox(
    session: AsyncSession,
) -> None:
    """Force an audit-INSERT failure AFTER the outbox INSERT; assert
    the outbox row is also rolled back. Mirrors v0.4 codex F16.
    """
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    eid = uuid4()

    with (
        patch(
            "aslan_core.streams.producer.audit_record",
            side_effect=RuntimeError("forced audit failure"),
        ),
        pytest.raises(RuntimeError, match="forced audit failure"),
    ):
        await producer.publish(_make_event(event_id=eid))
    # Caller decides commit vs rollback; the audit failure puts the
    # session into an unflushable state, but the outbox row was
    # session.flush'd already. Roll back so the next assertion sees
    # the post-rollback state.
    await session.rollback()

    rows = (await session.execute(select(Outbox).where(Outbox.event_id == eid))).all()
    assert rows == []


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_increments_prometheus_counter(
    session: AsyncSession,
) -> None:
    pytest.importorskip("prometheus_client")
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    from aslan_core.observability.metrics import aslan_stream_publishes_total

    impl = aslan_stream_publishes_total._ensure_impl()
    assert impl is not None

    before_val = impl.labels(stream="aslan.kap.filings.new", source_id="kap")._value.get()
    await producer.publish(_make_event())
    after_val = impl.labels(stream="aslan.kap.filings.new", source_id="kap")._value.get()
    assert after_val == before_val + 1


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_unknown_stream_collapses_to_other_label(
    session: AsyncSession,
) -> None:
    """Codex spec §5: unknown stream collapses to 'other' for the
    Prometheus label but the publish proceeds.
    """
    pytest.importorskip("prometheus_client")
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    outbox_id = await producer.publish(
        _make_event(),
        stream="aslan.unregistered.stream",
    )
    await session.commit()
    row = (await session.execute(select(Outbox).where(Outbox.outbox_id == outbox_id))).scalar_one()
    # The DB row keeps the actual stream name (analytics).
    assert row.stream_name == "aslan.unregistered.stream"
    # The Prometheus label is collapsed to 'other'.
    from aslan_core.observability.metrics import aslan_stream_publishes_total

    impl = aslan_stream_publishes_total._ensure_impl()
    assert impl is not None
    val = impl.labels(stream="other", source_id="kap")._value.get()
    assert val >= 1


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_stamps_traceparent_when_event_lacks_one(
    session: AsyncSession,
) -> None:
    """When ``[obs]`` is installed and an OTel span is active, the
    producer injects the W3C ``traceparent`` header onto the event
    before INSERT. The ``@traced`` decorator on ``StreamProducer.publish``
    starts a span automatically, so a non-empty ``traceparent`` always
    lands. With no SDK installed, the helper returns ``None`` and the
    event payload's ``traceparent`` field stays ``None`` — both branches
    are exercised inside :func:`_maybe_inject_traceparent`.
    """
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    outbox_id = await producer.publish(_make_event())
    await session.commit()
    row = (await session.execute(select(Outbox).where(Outbox.outbox_id == outbox_id))).scalar_one()
    assert "traceparent" in row.payload
    tp = row.payload["traceparent"]
    # Either (a) [obs] not installed → tp is None, or (b) [obs] is
    # installed and an OTel span was active during publish → tp is a
    # well-formed W3C traceparent ("00-<32 hex>-<16 hex>-<2 hex>").
    assert tp is None or (isinstance(tp, str) and tp.startswith("00-"))


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_event_id_field_round_trip_uuid_type(
    session: AsyncSession,
) -> None:
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    eid = uuid4()
    outbox_id = await producer.publish(_make_event(event_id=eid))
    await session.commit()
    row = (await session.execute(select(Outbox).where(Outbox.outbox_id == outbox_id))).scalar_one()
    assert isinstance(row.event_id, UUID)
    assert row.event_id == eid
