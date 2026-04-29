"""ORM round-trip tests for streams.* tables (v0.5.0 Task 7).

ORM models in ``aslan_core.models`` are NOT public API per CLAUDE.md;
these tests exercise the internal mapping against the migrated schema
to catch type-mismatches between SQLAlchemy column types and the
actual Postgres columns.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.models.streams import (
    DeadletterLog,
    DeadletterRedisIndex,
    DeadletterXaddIntent,
    EventIdToRedis,
    Outbox,
    RedactionRegistry,
)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _wipe_streams(session: AsyncSession) -> AsyncIterator[None]:
    """Clean up streams.* rows after each test so other modules' _wipe()
    helpers can DELETE FROM src.ingestion_run without hitting our FK
    constraints. The streams subsystem is self-cleaning per-module."""
    yield
    for stmt in [
        "DELETE FROM streams.event_id_to_redis",
        "DELETE FROM streams.deadletter_xadd_intent",
        "DELETE FROM streams.deadletter_redis_index",
        "DELETE FROM streams.deadletter_log",
        "DELETE FROM streams.redaction_registry",
        "DELETE FROM streams.outbox",
    ]:
        await session.execute(text(stmt))
    await session.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def _seed_run(session: AsyncSession) -> AsyncIterator[int]:
    await session.execute(
        text(
            "INSERT INTO src.source(source_id, name, kind, license_status) "
            "VALUES('kap', 't', 'scraper', 'open') ON CONFLICT DO NOTHING"
        )
    )
    await session.execute(
        text(
            "INSERT INTO src.ingestion_run(source_id, job_name, started_at, status) "
            "VALUES('kap', 'test', now(), 'success') ON CONFLICT DO NOTHING"
        )
    )
    rid: int = (
        await session.execute(text("SELECT ingestion_run_id FROM src.ingestion_run LIMIT 1"))
    ).scalar_one()
    await session.commit()
    yield rid


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_round_trip(session: AsyncSession, _seed_run: int) -> None:
    rid = _seed_run
    eid = uuid4()
    row = Outbox(
        stream_name="aslan.kap.filings.new",
        event_id=eid,
        schema_version=1,
        payload={"k": "v"},
        producer_run_id=rid,
        source_id="kap",
        actor_id="user:test",
        actor_kind="user",
    )
    session.add(row)
    await session.commit()
    fetched = (await session.execute(select(Outbox).where(Outbox.event_id == eid))).scalar_one()
    assert fetched.stream_name == "aslan.kap.filings.new"
    assert fetched.payload == {"k": "v"}
    assert fetched.published_at is None
    assert fetched.publish_attempts == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_deadletter_log_round_trip(session: AsyncSession) -> None:
    eid = uuid4()
    row = DeadletterLog(
        stream_name="s",
        deadletter_stream="s.deadletter",
        event_id=eid,
        original_message_id=f"rid-orm-{uuid4()}",
        group_name="g",
        consumer_name="c",
        failure_count=5,
        last_error="boom",
    )
    session.add(row)
    await session.commit()
    assert row.failure_id is not None
    assert row.routed_at_redis is None  # NULL until step 4 of routing


@pytest.mark.asyncio(loop_scope="session")
async def test_deadletter_redis_index_round_trip(session: AsyncSession) -> None:
    log = DeadletterLog(
        stream_name="s",
        deadletter_stream="s.deadletter",
        event_id=uuid4(),
        original_message_id=f"rid-orm-{uuid4()}",
        group_name="g",
        consumer_name="c",
        failure_count=5,
        last_error="boom",
    )
    session.add(log)
    await session.flush()
    idx = DeadletterRedisIndex(
        failure_id=log.failure_id,
        redis_message_id=f"1714492800000-{uuid4()}",
    )
    session.add(idx)
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_deadletter_xadd_intent_round_trip(session: AsyncSession) -> None:
    log = DeadletterLog(
        stream_name="s",
        deadletter_stream="s.deadletter",
        event_id=uuid4(),
        original_message_id=f"rid-orm-{uuid4()}",
        group_name="g",
        consumer_name="c",
        failure_count=5,
        last_error="boom",
    )
    session.add(log)
    await session.flush()
    intent = DeadletterXaddIntent(
        failure_id=log.failure_id,
        stream_name="s",
        redis_lower_bound_id="0-0",
        owner_id="host:1:abc",
    )
    session.add(intent)
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_event_id_to_redis_composite_pk_round_trip(session: AsyncSession) -> None:
    eid = uuid4()
    a = EventIdToRedis(
        event_id=eid,
        stream_name="s",
        redis_message_id="rid-1",
    )
    session.add(a)
    await session.commit()
    fetched = (
        await session.execute(select(EventIdToRedis).where(EventIdToRedis.event_id == eid))
    ).scalar_one()
    assert fetched.redis_message_id == "rid-1"
    assert fetched.redacted_at is None


@pytest.mark.asyncio(loop_scope="session")
async def test_redaction_registry_round_trip(session: AsyncSession) -> None:
    """Insert via ORM (running as the migration / superuser role; not as
    aslan_app — the REVOKE/GRANT contract is exercised in
    test_migration_0018_streams_redaction_registry.py)."""
    eid = uuid4()
    row = RedactionRegistry(
        event_id=eid,
        redaction_reason="Art.17",
        original_stream="aslan.kap.filings.new",
        redacted_payload={"redacted": True},
        redacted_payload_hash="a" * 64,
        original_payload_hash="b" * 64,
    )
    session.add(row)
    await session.commit()
