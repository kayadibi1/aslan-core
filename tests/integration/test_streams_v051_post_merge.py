"""v0.5.1 post-merge regression coverage.

Each test pins one of the bugs that codex's adversarial review found
after v0.5.0 shipped. The fixes live alongside the original code paths;
this file is the dedicated home for proofs that the fixes hold.

Findings covered:

* **F1 (HIGH)** — ``write_registry_entry`` no longer primes the Redis
  redaction cache, so a caller rollback cannot leak a phantom redaction
  to consumers for ``REDACTION_CACHE_TTL_SECONDS``.
* **F2 (HIGH)** — ``stream_deadletter_janitor`` pass 2 reuses the
  canonical Redis id from ``streams.deadletter_redis_index`` (or scans
  the dead-letter stream for an unindexed XADD) instead of XADDing a
  duplicate that pass 4 would later XDEL.
* **F-outbox (HIGH)** — every successful XADD is durably indexed in
  ``streams.event_id_to_redis`` even when the per-row savepoint later
  rolls back.
* **F4 (MEDIUM)** — the consumer ack commits its audit row BEFORE the
  irreversible Redis SADD/DEL/XACK transition.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.streams import (
    FilingNewEvent,
    StreamProducer,
    canonical_payload_hash,
    drain_outbox,
    stream_deadletter_janitor,
    write_registry_entry,
)
from aslan_core.streams.redaction import redaction_cache_key

pytestmark = pytest.mark.integration


STREAM = "aslan.kap.filings.new"
GROUP = "internal-test"


# ── F1 regression ──────────────────────────────────────────────────


async def test_f1_registry_write_does_not_prime_cache_before_commit(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """If the caller rolls back after ``write_registry_entry``, the Redis
    redaction cache must NOT advertise the registry row — otherwise
    consumers would honour a redaction the database never committed for
    up to ``REDACTION_CACHE_TTL_SECONDS`` (24h).
    """
    eid = uuid4()
    payload = {"x": 1}
    cache_key = redaction_cache_key(eid)
    await redis_client.delete(cache_key)

    async with session_factory() as s:
        await write_registry_entry(
            s,
            event_id=eid,
            redaction_reason="Art.17",
            original_stream=STREAM,
            redacted_payload=payload,
            redacted_payload_hash=canonical_payload_hash(payload),
            original_payload_hash=canonical_payload_hash({"x": 0}),
        )
        await s.rollback()

    cached = await redis_client.get(cache_key)
    assert cached is None, "redaction cache must not contain a row whose Postgres tx rolled back"

    # The Postgres registry MUST also be empty (rollback honoured).
    seen = (
        await session.execute(
            text("SELECT 1 FROM streams.redaction_registry WHERE event_id = :eid"),
            {"eid": str(eid)},
        )
    ).scalar_one_or_none()
    assert seen is None


# ── F2 (janitor pass 2) regression ─────────────────────────────────


@pytest_asyncio.fixture
async def _wipe_deadletter_state(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    yield
    async with session_factory() as s:
        await s.execute(text("DELETE FROM streams.deadletter_xadd_intent"))
        await s.execute(text("DELETE FROM streams.deadletter_redis_index"))
        await s.execute(text("DELETE FROM streams.deadletter_log"))
        await s.commit()
    await redis_client.delete(f"{STREAM}.deadletter")


async def _insert_stuck_deadletter_row(
    session: AsyncSession,
    *,
    event_id: str,
    routed_age_seconds: int = 600,
) -> int:
    fid = (
        await session.execute(
            text(
                """
                INSERT INTO streams.deadletter_log (
                    stream_name, deadletter_stream, event_id,
                    original_message_id, group_name, consumer_name,
                    failure_count, last_error, payload_excerpt, routed_at,
                    routed_at_redis, redis_message_id
                ) VALUES (
                    :stream, :deadletter_stream, :event_id,
                    :original_message_id, :group_name, 'consumer:test',
                    5, 'boom', '{}'::jsonb,
                    now() - (:routed_age_seconds * INTERVAL '1 second'),
                    NULL,
                    NULL
                )
                RETURNING failure_id
                """
            ),
            {
                "stream": STREAM,
                "deadletter_stream": f"{STREAM}.deadletter",
                "event_id": event_id,
                "original_message_id": "stuck-0",
                "group_name": GROUP,
                "routed_age_seconds": routed_age_seconds,
            },
        )
    ).scalar_one()
    await session.commit()
    return int(fid)


async def test_f2_pass_2_reuses_indexed_canonical_id_instead_of_xadding(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _wipe_deadletter_state: None,
) -> None:
    """If a prior consumer XADDed AND indexed but crashed before
    UPDATEing ``deadletter_log``, pass 2 must reuse the canonical Redis
    id rather than XADD a duplicate. The duplicate would later be
    XDELed by pass 4 as an orphan, leaving ``deadletter_log`` pointing
    at a deleted Redis id.
    """
    fid = await _insert_stuck_deadletter_row(
        session,
        event_id="00000000-0000-0000-0000-000000000060",
    )
    canonical_message_id = await redis_client.xadd(
        f"{STREAM}.deadletter",
        {
            "event_id": "00000000-0000-0000-0000-000000000060",
            "failure_id": str(fid),
            "stream_name": STREAM,
        },
    )
    await session.execute(
        text(
            "INSERT INTO streams.deadletter_redis_index "
            "(failure_id, redis_message_id) VALUES (:fid, :rid)"
        ),
        {"fid": fid, "rid": canonical_message_id},
    )
    await session.commit()
    xlen_before = await redis_client.xlen(f"{STREAM}.deadletter")

    await stream_deadletter_janitor(
        redis=redis_client,
        session_factory=session_factory,
        once=True,
    )

    # Stream length unchanged — no duplicate XADD.
    assert await redis_client.xlen(f"{STREAM}.deadletter") == xlen_before

    log = (
        await session.execute(
            text(
                "SELECT redis_message_id, routed_at_redis "
                "FROM streams.deadletter_log WHERE failure_id = :fid"
            ),
            {"fid": fid},
        )
    ).one()
    assert log.redis_message_id == canonical_message_id
    assert log.routed_at_redis is not None

    idx_rid = (
        await session.execute(
            text(
                "SELECT redis_message_id FROM streams.deadletter_redis_index "
                "WHERE failure_id = :fid"
            ),
            {"fid": fid},
        )
    ).scalar_one()
    assert idx_rid == canonical_message_id


async def test_f2_pass_2_indexes_unindexed_redis_orphan_instead_of_xadding(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _wipe_deadletter_state: None,
) -> None:
    """If a prior consumer XADDed but crashed before INSERTing the
    index row, pass 2 must scan the dead-letter stream, find the
    unindexed XADD, index it, and reuse it — not XADD a duplicate.
    """
    fid = await _insert_stuck_deadletter_row(
        session,
        event_id="00000000-0000-0000-0000-000000000061",
    )
    orphan_message_id = await redis_client.xadd(
        f"{STREAM}.deadletter",
        {
            "event_id": "00000000-0000-0000-0000-000000000061",
            "failure_id": str(fid),
            "stream_name": STREAM,
        },
    )
    xlen_before = await redis_client.xlen(f"{STREAM}.deadletter")

    await stream_deadletter_janitor(
        redis=redis_client,
        session_factory=session_factory,
        once=True,
    )

    assert await redis_client.xlen(f"{STREAM}.deadletter") == xlen_before
    log = (
        await session.execute(
            text(
                "SELECT redis_message_id, routed_at_redis "
                "FROM streams.deadletter_log WHERE failure_id = :fid"
            ),
            {"fid": fid},
        )
    ).one()
    assert log.redis_message_id == orphan_message_id
    assert log.routed_at_redis is not None

    idx_rid = (
        await session.execute(
            text(
                "SELECT redis_message_id FROM streams.deadletter_redis_index "
                "WHERE failure_id = :fid"
            ),
            {"fid": fid},
        )
    ).scalar_one()
    assert idx_rid == orphan_message_id


# ── F-outbox regression ────────────────────────────────────────────


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
        "title": "post-merge fixture",
        "published_at": datetime.now(UTC),
        "primary_object_key": "k",
        "bucket": "b",
        "is_revision": False,
        "revision_no": 1,
    }
    base.update(override)
    return FilingNewEvent(**base)


class _CrashAfterIndexCommitRedis:
    """Redis wrapper that forces the SAVEPOINT path to fail by raising
    in ``XADD`` the second time it's called. The first XADD lands AND
    is indexed by the new independent-session write; the second forces
    a savepoint rollback so we exercise the post-XADD failure shape
    where the outbox row stays pending.
    """

    def __init__(self, real: Any) -> None:
        self._real = real
        self._calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def xadd(self, *args: Any, **kwargs: Any) -> Any:
        self._calls += 1
        if self._calls == 2:
            raise RuntimeError("simulated savepoint-failing XADD path")
        return await self._real.xadd(*args, **kwargs)


async def test_f_outbox_indexes_xadd_even_on_savepoint_rollback(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every successful XADD MUST have a matching ``event_id_to_redis``
    row, even if the per-row savepoint later rolls back. Without the
    independent-session index write, a savepoint failure leaves the
    Redis copy invisible to GDPR Art. 17 redaction lookup.
    """
    async with session_factory() as s:
        await s.execute(text("DELETE FROM streams.event_id_to_redis"))
        await s.execute(text("DELETE FROM streams.outbox"))
        await s.execute(
            text(
                "INSERT INTO src.source(source_id, name, kind, license_status) "
                "VALUES('kap','KAP','scraper','open') ON CONFLICT DO NOTHING"
            )
        )
        rid: int = (
            await s.execute(
                text(
                    "INSERT INTO src.ingestion_run(source_id, job_name, status) "
                    "VALUES('kap','f_outbox','succeeded') RETURNING ingestion_run_id"
                )
            )
        ).scalar_one()
        await s.commit()
    await redis_client.delete(STREAM)

    eid_a = uuid4()
    eid_b = uuid4()
    async with session_factory() as s:
        producer = StreamProducer(session=s, ingestion_run_id=rid)
        await producer.publish(_ev(event_id=eid_a, producer_run_id=rid))
        await producer.publish(_ev(event_id=eid_b, producer_run_id=rid))
        await s.commit()

    crashing = _CrashAfterIndexCommitRedis(redis_client)
    await drain_outbox(
        redis=crashing,  # type: ignore[arg-type]
        session_factory=session_factory,
        once=True,
    )

    # The first XADD landed in Redis — every Redis entry must be indexed.
    redis_entries = await redis_client.xrange(STREAM, min="-", max="+")
    redis_ids = {entry_id for entry_id, _ in redis_entries}
    assert len(redis_ids) >= 1

    indexed = (
        (
            await session.execute(
                text(
                    "SELECT redis_message_id FROM streams.event_id_to_redis WHERE stream_name = :s"
                ),
                {"s": STREAM},
            )
        )
        .scalars()
        .all()
    )
    indexed_ids = set(indexed)
    assert redis_ids.issubset(indexed_ids), (
        f"every Redis copy must be indexed for redaction lookup: "
        f"missing={redis_ids - indexed_ids!r}"
    )


# ── F4 (consumer ack ordering) regression ──────────────────────────


async def test_f4_audit_committed_before_redis_transition(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The audit row for a consumed event must commit BEFORE the Lua
    SADD/DEL/XACK transition. We can prove this by injecting a Lua
    failure: the audit row is present in Postgres, but Redis still has
    the message claimed and unacked. Without the F4 reorder, the audit
    would be missing entirely while Redis showed it consumed.
    """
    from aslan_core.streams import StreamConsumer

    async with session_factory() as s:
        await s.execute(text("DELETE FROM streams.event_id_to_redis"))
        await s.execute(text("DELETE FROM streams.outbox"))
        await s.execute(
            text(
                "INSERT INTO src.source(source_id, name, kind, license_status) "
                "VALUES('kap','KAP','scraper','open') ON CONFLICT DO NOTHING"
            )
        )
        rid_int: int = (
            await s.execute(
                text(
                    "INSERT INTO src.ingestion_run(source_id, job_name, status) "
                    "VALUES('kap','f4','succeeded') RETURNING ingestion_run_id"
                )
            )
        ).scalar_one()
        await s.commit()
    await redis_client.delete(STREAM)

    eid = uuid4()
    async with session_factory() as s:
        producer = StreamProducer(session=s, ingestion_run_id=rid_int)
        await producer.publish(_ev(event_id=eid, producer_run_id=rid_int))
        await s.commit()
    await drain_outbox(
        redis=redis_client,
        session_factory=session_factory,
        once=True,
    )

    class _BrokenLuaRedis:
        """Wrapper that fails ``eval`` (the Lua call inside _ack)."""

        def __init__(self, real: Any) -> None:
            self._real = real

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

        async def eval(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("simulated Lua eval failure")

    consumer = StreamConsumer(
        redis=_BrokenLuaRedis(redis_client),  # type: ignore[arg-type]
        session_factory=session_factory,
        max_supported_schema_version=1,
    )
    await consumer.ensure_group(STREAM, GROUP)

    with pytest.raises(RuntimeError, match="simulated Lua eval failure"):
        async for _evt, ack in consumer.consume(
            STREAM,
            GROUP,
            "f4-consumer",
            block_ms=200,
        ):
            await ack()
            break

    # Audit was committed BEFORE the failing Lua call.
    audit_count = (
        await session.execute(
            text(
                """
                SELECT count(*) FROM audit.events
                WHERE operation IN ('stream.consume_ack', 'stream.consumed_redacted')
                  AND target_pk @> CAST(:tp AS JSONB)
                """
            ),
            {"tp": json.dumps({"event_id": str(eid)})},
        )
    ).scalar_one()
    assert audit_count == 1, (
        "F4 contract: audit must commit before the Redis transition, so "
        "a failing Lua call still leaves a Postgres consume audit row"
    )
