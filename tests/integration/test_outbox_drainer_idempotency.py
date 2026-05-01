"""Outbox-drainer idempotency proof under crash injection (Task 11 of v0.5.0).

Codex spec §6 + critical-contract item 1 — the headline correctness
guarantee that the v0.5.0 streams design hinges on:

  Crashing the drainer between XADD success and the SQL UPDATE that
  flips ``published_at`` is the WORST-case retry shape. On retry the
  outbox row is still pending and will be re-XADD'd, producing a
  duplicate Redis entry for the same ``event_id``.

  The contract: end-to-end ``event_id`` dedup at the consumer side
  (codex F1+F4 atomic claim, landing in Tasks 12-14) collapses every
  duplicate Redis entry to a single ack. Tests below simulate the
  consumer dedup as a manual ``XRANGE`` + dedup-on-event_id pass —
  the full StreamConsumer arrives in subsequent tasks.

This task introduces NO new production code; it locks the contract
that subsequent consumer work must preserve.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.models.streams import Outbox
from aslan_core.streams import FilingNewEvent, StreamProducer
from aslan_core.streams.outbox_drainer import drain_outbox

pytestmark = pytest.mark.integration


def _ev() -> FilingNewEvent:
    return FilingNewEvent(
        schema_version=1,
        event_id=uuid4(),
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
                "VALUES('kap','outbox_drainer_idempotency','succeeded') "
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


class _CrashAfterXAddRedis:
    """Wraps the real Redis client; on the Nth ``xadd`` call, raises an
    exception AFTER Redis has accepted the entry — simulating the
    drainer crash between XADD return and the SQL UPDATE that flips
    ``published_at``.

    All other Redis calls forward to the wrapped client unchanged so
    the drainer's gauge / count / range queries still work.
    """

    def __init__(self, real: Redis, crash_on_xadd: int = 1) -> None:
        self._real = real
        self._n = 0
        self._crash = crash_on_xadd

    async def xadd(self, *args: Any, **kwargs: Any) -> str:
        result: str = await self._real.xadd(*args, **kwargs)
        self._n += 1
        if self._n == self._crash:
            raise RuntimeError("simulated drainer crash post-XADD")
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@pytest.mark.asyncio(loop_scope="session")
async def test_drainer_crash_after_xadd_recovers_with_one_consumer_ack(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    """Codex critical-contract item 1: kill the drainer between XADD
    success and outbox UPDATE; the second drainer attempt re-XADDs the
    same row, producing TWO Redis entries for the same event_id —
    consumer-side dedup collapses to ONE ack.
    """
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    ev = _ev()
    await producer.publish(ev)
    await session.commit()

    # Attempt 1 — drainer crashes after XADD; outbox row stays pending.
    faulty = _CrashAfterXAddRedis(redis_client, crash_on_xadd=1)
    await drain_outbox(
        redis=faulty,  # type: ignore[arg-type]
        session_factory=session_factory,
        once=True,
    )

    # The drainer's SAVEPOINT error path committed via its own session;
    # expire ours so the next SELECT re-fetches the post-drainer state.
    session.expire_all()

    # Outbox still pending; Redis has 1 entry; the row's
    # publish_attempts was bumped by the drainer's per-row SAVEPOINT
    # error path.
    rows = (await session.execute(select(Outbox))).scalars().all()
    assert len(rows) == 1
    assert rows[0].published_at is None
    assert rows[0].publish_attempts == 1

    entries = await redis_client.xrange("aslan.kap.filings.new", min="-", max="+")
    assert len(entries) == 1

    # Attempt 2 — clean run; XADDs the SAME outbox row a second time.
    await drain_outbox(
        redis=redis_client,
        session_factory=session_factory,
        once=True,
    )
    session.expire_all()

    rows2 = (await session.execute(select(Outbox))).scalars().all()
    assert rows2[0].published_at is not None
    entries2 = await redis_client.xrange("aslan.kap.filings.new", min="-", max="+")
    assert len(entries2) == 2

    # Now simulate a consumer that dedups on event_id — exactly ONE ack.
    seen: set[str] = set()
    acks = 0
    for _, fields in entries2:
        eid = fields["event_id"]
        if eid in seen:
            continue
        seen.add(eid)
        acks += 1
    assert acks == 1, "consumer dedup on event_id must yield exactly ONE ack"


@pytest.mark.asyncio(loop_scope="session")
async def test_drainer_crash_loop_10x_still_one_ack(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    """Codex headline test: 10 crash-induced retries → 11 Redis entries
    → 1 consumer ack. The contract is: end-to-end ``event_id`` dedup at
    the consumer side is sufficient; the producer / drainer side need
    not provide exactly-once delivery.
    """
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    ev = _ev()
    await producer.publish(ev)
    await session.commit()

    for _ in range(10):
        faulty = _CrashAfterXAddRedis(redis_client, crash_on_xadd=1)
        await drain_outbox(
            redis=faulty,  # type: ignore[arg-type]
            session_factory=session_factory,
            once=True,
        )

    # 11th attempt: clean run.
    await drain_outbox(
        redis=redis_client,
        session_factory=session_factory,
        once=True,
    )

    entries = await redis_client.xrange("aslan.kap.filings.new", min="-", max="+")
    assert len(entries) == 11
    seen: set[str] = set()
    for _, fields in entries:
        seen.add(fields["event_id"])
    assert len(seen) == 1, "all 11 entries share the same event_id"


@pytest.mark.asyncio(loop_scope="session")
async def test_drainer_crash_does_not_leak_actor_contextvar(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    """Defensive: even when the drainer's per-row XADD path raises, the
    daemon's try/finally must restore the previous ContextVar actor
    on exit so the test's autouse default actor (``user:pytest``) is
    still in effect after the call returns.
    """
    from aslan_core.audit import current_actor

    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    await producer.publish(_ev())
    await session.commit()

    # Sanity: the autouse fixture set user:pytest before this test.
    pre_actor = current_actor()
    assert pre_actor is not None
    assert pre_actor.actor_id == "user:pytest"

    faulty = _CrashAfterXAddRedis(redis_client, crash_on_xadd=1)
    await drain_outbox(
        redis=faulty,  # type: ignore[arg-type]
        session_factory=session_factory,
        once=True,
    )

    # The drainer set system:streams.outbox_drainer during its run; on
    # exit, it must restore user:pytest.
    post_actor = current_actor()
    assert post_actor is not None
    assert post_actor.actor_id == "user:pytest"


@pytest.mark.asyncio(loop_scope="session")
async def test_drainer_crash_after_xadd_keeps_outbox_pending(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    _flush_redis: None,
) -> None:
    """Spec-shape regression: after the XADD-then-crash sequence the
    outbox row's ``published_at`` MUST still be NULL — otherwise the
    contract is broken (a half-published row is observable to consumers
    as a complete publish).
    """
    rid = await _seed_run(session)
    producer = StreamProducer(session=session, ingestion_run_id=rid)
    ev = _ev()
    await producer.publish(ev)
    await session.commit()

    faulty = _CrashAfterXAddRedis(redis_client, crash_on_xadd=1)
    await drain_outbox(
        redis=faulty,  # type: ignore[arg-type]
        session_factory=session_factory,
        once=True,
    )

    # Drainer's SAVEPOINT error path committed via a sibling session;
    # expire ours so SELECT re-fetches post-drainer state.
    session.expire_all()

    # Reload from DB.
    row = (await session.execute(select(Outbox).where(Outbox.event_id == ev.event_id))).scalar_one()
    assert row.published_at is None, "outbox row must stay pending after a post-XADD drainer crash"
    assert row.redis_message_id is None
    # last_error captured the failure for forensic dashboards.
    assert row.last_error is not None
    assert "simulated drainer crash post-XADD" in row.last_error
