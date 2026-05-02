"""Migration 0020 must not regress any workflow that pre-dates it.

Spec §8.2 + codex round-5: migration 0020's REVOKE list is narrow
(``streams.redaction_registry_insert`` EXECUTE from PUBLIC and
``aslan_dashboard``). It must not break:

(a) ``streams.redaction_registry_insert(...)`` — the v0.5 SECURITY
    DEFINER mutation surface — must still be callable from
    ``SET ROLE aslan_app``. The spec named this role explicitly because
    0018 GRANTs EXECUTE only to aslan_app and the round-9 finding
    locked down OWNER. This sub-test calls the function under
    ``SET ROLE aslan_app`` to exercise the role-boundary path.
(b) The ``audit.observation_batch_keys`` deferred parent-check
    constraint trigger from migration 0014 — must still raise
    ``foreign_key_violation`` at COMMIT for orphan inserts and accept
    parented inserts. Run from the test session (the testcontainer
    superuser); ``aslan_app`` has no INSERT on this table by design
    (the role exists for SECURITY DEFINER ownership, not as a writer).
    The post-0020 contract being verified here is trigger correctness,
    not role privilege.
(c) The v0.5 outbox drainer's read+update workflow — must still drain
    a seeded outbox row end-to-end. Smoke check that 0020 didn't
    accidentally rename or drop a column the drainer reads.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.streams import FilingNewEvent, StreamProducer
from aslan_core.streams.outbox_drainer import drain_outbox

pytestmark = pytest.mark.integration


def _async_dsn_to_asyncpg(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


# ── (a) redaction_registry_insert under SET ROLE aslan_app ──


@pytest.mark.asyncio(loop_scope="session")
async def test_redaction_registry_insert_executable_under_aslan_app(
    pg_dsn: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    eid = uuid4()
    conn = await asyncpg.connect(dsn=_async_dsn_to_asyncpg(pg_dsn))
    try:
        await conn.execute("SET ROLE aslan_app")
        await conn.execute(
            "SELECT streams.redaction_registry_insert("
            "  $1::uuid, 'Art.17', now(), 'kap', "
            "  '{}'::jsonb, repeat('a', 64), repeat('b', 64))",
            eid,
        )
        await conn.execute("RESET ROLE")
    finally:
        await conn.close()
    async with session_factory() as s:
        await s.execute(
            text("DELETE FROM streams.redaction_registry WHERE event_id = :eid"),
            {"eid": eid},
        )
        await s.commit()


# ── (b) audit.observation_batch_keys deferred parent-check trigger ──


@pytest_asyncio.fixture(loop_scope="session")
async def _seeded_audit_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[int, datetime]]:
    """Insert an ``audit.events`` parent row for the trigger test.
    Returns the composite PK ``(event_id, occurred_at)``."""
    async with session_factory() as s:
        result = await s.execute(
            text(
                "INSERT INTO audit.events "
                "(actor_id, actor_kind, operation, target_schema, target_table, "
                " target_pk) "
                "VALUES ('user-trigger-test', 'user', 'insert', 'ts', 'observation', "
                "        CAST(:pk_json AS jsonb)) "
                "RETURNING event_id, occurred_at"
            ),
            {"pk_json": '{"x":1}'},
        )
        ev_id, ev_ts = result.one()
        await s.commit()
    yield (ev_id, ev_ts)
    async with session_factory() as s:
        await s.execute(
            text("DELETE FROM audit.events WHERE event_id = :eid AND occurred_at = :ts"),
            {"eid": ev_id, "ts": ev_ts},
        )
        await s.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_batch_keys_trigger_accepts_parented_insert(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    _seeded_audit_event: tuple[int, datetime],
) -> None:
    """Parent ``audit.events`` row exists → deferred trigger passes at
    commit. Verifies the trigger from migration 0014 is still wired
    up correctly post-0020."""
    ev_id, ev_ts = _seeded_audit_event
    series_id = 999_001
    ts_val = datetime.now(UTC)
    as_of = datetime.now(UTC)
    payload_hash = "f" * 64

    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO audit.observation_batch_keys "
                "(event_id, occurred_at, series_id, ts, as_of, payload_hash, action) "
                "VALUES (:eid, :occ, :sid, :tsv, :asof, :ph, 'inserted')"
            ),
            {
                "eid": ev_id,
                "occ": ev_ts,
                "sid": series_id,
                "tsv": ts_val,
                "asof": as_of,
                "ph": payload_hash,
            },
        )
        await s.commit()

    async with session_factory() as s:
        await s.execute(
            text(
                "DELETE FROM audit.observation_batch_keys "
                "WHERE event_id = :eid AND occurred_at = :occ AND series_id = :sid "
                "AND ts = :tsv AND as_of = :asof"
            ),
            {
                "eid": ev_id,
                "occ": ev_ts,
                "sid": series_id,
                "tsv": ts_val,
                "asof": as_of,
            },
        )
        await s.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_batch_keys_trigger_rejects_orphan_insert(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No matching ``audit.events`` parent → deferred trigger raises
    foreign-key violation at commit. SQLSTATE 23503."""
    from sqlalchemy.exc import IntegrityError

    missing_event_id = 2_147_483_640
    missing_ts = datetime.now(UTC)
    series_id = 999_002
    payload_hash = "e" * 64

    with pytest.raises(IntegrityError):
        async with session_factory() as s:
            await s.execute(
                text(
                    "INSERT INTO audit.observation_batch_keys "
                    "(event_id, occurred_at, series_id, ts, as_of, payload_hash, "
                    " action) "
                    "VALUES (:eid, :occ, :sid, :tsv, :asof, :ph, 'inserted')"
                ),
                {
                    "eid": missing_event_id,
                    "occ": missing_ts,
                    "sid": series_id,
                    "tsv": missing_ts,
                    "asof": missing_ts,
                    "ph": payload_hash,
                },
            )
            await s.commit()


# ── (c) outbox drainer smoke test ──


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_drainer_runs_once_post_migration_0020(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    """Seed one outbox row, run the drainer once, assert ``published_at``
    is set and an ``event_id_to_redis`` mapping exists. Proves
    migration 0020 didn't accidentally rename/drop a column the
    drainer reads."""
    await session.execute(text("DELETE FROM streams.event_id_to_redis"))
    await session.execute(text("DELETE FROM streams.outbox"))
    await session.execute(
        text(
            "INSERT INTO src.source(source_id, name, kind, license_status) "
            "VALUES('kap', 'KAP', 'scraper', 'open') ON CONFLICT DO NOTHING"
        )
    )
    run_id: int = (
        await session.execute(
            text(
                "INSERT INTO src.ingestion_run(source_id, job_name, status) "
                "VALUES('kap', 'aslan_app_workflows', 'succeeded') "
                "RETURNING ingestion_run_id"
            )
        )
    ).scalar_one()
    await session.commit()

    producer = StreamProducer(session=session, ingestion_run_id=run_id)
    event = FilingNewEvent(
        schema_version=1,
        event_id=uuid4(),
        produced_at=datetime.now(UTC),
        producer_run_id=run_id,
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
    await producer.publish(event)
    await session.commit()

    await drain_outbox(
        redis=redis_client,
        session_factory=session_factory,
        once=True,
        poll_interval_s=0.0,
    )

    async with session_factory() as s:
        published = (
            await s.execute(
                text("SELECT published_at FROM streams.outbox WHERE event_id = :eid"),
                {"eid": event.event_id},
            )
        ).scalar_one()
        assert published is not None, "drainer did not flip published_at after one pass"

        mapping_count = (
            await s.execute(
                text("SELECT count(*) FROM streams.event_id_to_redis WHERE event_id = :eid"),
                {"eid": event.event_id},
            )
        ).scalar_one()
        assert mapping_count >= 1, "drainer did not record event_id_to_redis mapping"
