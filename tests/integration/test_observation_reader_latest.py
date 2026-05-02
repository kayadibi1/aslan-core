"""Integration tests — ``ObservationReader.latest``.

Plan reference: v0.4.0 Task 21. Canonical PIT pattern:

    SELECT DISTINCT ON (series_id, ts) *
    FROM ts.observation
    WHERE series_id = :sid
      AND (:pit IS NULL OR as_of <= :pit)
    ORDER BY series_id, ts DESC, as_of DESC
    LIMIT 1

For each ``(series_id, ts)`` only the row with max ``as_of <= pit``
survives the DISTINCT ON; the outer LIMIT 1 picks the highest ``ts``.

Required regression tests (codex F1 + F17, 2026-04-29):
- F1: same ``(series_id, ts)`` with two ``as_of`` values; PIT between
  returns the original; PIT after both returns the restatement.
- F17: an OLDER ts whose RESTATEMENT carries the globally-largest
  ``as_of``, plus a NEWER ts that only has its initial as_of. ``ts``
  priority must come BEFORE ``as_of`` priority — the NEWER ts wins.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import Actor, set_actor
from aslan_core.schemas.timeseries import ObservationIn
from aslan_core.timeseries import ObservationReader, ObservationWriter

pytestmark = pytest.mark.integration


async def _wipe(session: AsyncSession) -> None:
    for stmt in [
        "DELETE FROM audit.observation_batch_keys",
        "DELETE FROM ts.observation",
        "DELETE FROM ts.series_subject",
        "DELETE FROM ts.series_catalog",
        "DELETE FROM audit.events",
    ]:
        await session.execute(text(stmt))
    await session.commit()


@pytest.fixture(autouse=True)
async def _cleanup_ts(session: AsyncSession) -> AsyncIterator[None]:
    yield
    await session.rollback()
    await _wipe(session)


async def _seed_sources(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('kap', 'KAP', 'scraper', 'open') "
            "ON CONFLICT DO NOTHING"
        )
    )
    await session.commit()


async def _new_run(session: AsyncSession, job: str) -> int:
    await _seed_sources(session)
    rid = await session.scalar(
        text(
            "INSERT INTO src.ingestion_run (source_id, job_name) "
            "VALUES ('kap', :job) RETURNING ingestion_run_id"
        ),
        {"job": job},
    )
    await session.commit()
    return int(rid)


async def _seed_series(session: AsyncSession, rid: int, *, code: str) -> int:
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    sr = await w.upsert_series(
        series_code=code,
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()
    return sr.series_id


@pytest.mark.asyncio(loop_scope="session")
async def test_latest_returns_none_for_empty_series(session: AsyncSession) -> None:
    rid = await _new_run(session, job="lat-empty")
    sid = await _seed_series(session, rid, code="l.empty")

    r = ObservationReader(session)
    assert await r.latest(sid) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_latest_returns_only_observation(session: AsyncSession) -> None:
    """A single-row series — ``latest()`` returns it."""
    rid = await _new_run(session, job="lat-single")
    sid = await _seed_series(session, rid, code="l.single")
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    await w.write(
        sid,
        [
            ObservationIn(
                ts=datetime(2026, 1, 1, tzinfo=UTC),
                as_of=datetime(2026, 1, 2, tzinfo=UTC),
                value=42.0,
            ),
        ],
    )
    await session.commit()

    r = ObservationReader(session)
    o = await r.latest(sid)
    assert o is not None
    assert o.value == 42.0
    assert o.ts == datetime(2026, 1, 1, tzinfo=UTC)
    assert o.as_of == datetime(2026, 1, 2, tzinfo=UTC)
    assert o.series_id == sid


@pytest.mark.asyncio(loop_scope="session")
async def test_latest_returns_most_recent_ts(session: AsyncSession) -> None:
    """Across multiple ts, ``latest()`` picks the highest ts."""
    rid = await _new_run(session, job="lat-multi")
    sid = await _seed_series(session, rid, code="l.multi")
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    await w.write(
        sid,
        [
            ObservationIn(
                ts=datetime(2026, 1, d, tzinfo=UTC),
                as_of=datetime(2026, 1, d + 1, tzinfo=UTC),
                value=float(d),
            )
            for d in range(1, 6)
        ],
    )
    await session.commit()

    r = ObservationReader(session)
    o = await r.latest(sid)
    assert o is not None
    assert o.ts == datetime(2026, 1, 5, tzinfo=UTC)
    assert o.value == 5.0


@pytest.mark.asyncio(loop_scope="session")
async def test_latest_pit_collapse_returns_correct_as_of(
    session: AsyncSession,
) -> None:
    """Codex F1 — write two rows for same (series_id, ts) with different
    ``as_of``. ``latest(as_of=between)`` returns the original;
    ``latest(as_of=after_both)`` returns the restated value;
    ``latest(as_of=before_any)`` returns ``None``.
    """
    rid = await _new_run(session, job="lat-pit")
    sid = await _seed_series(session, rid, code="l.pit")
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)

    ts = datetime(2026, 1, 1, tzinfo=UTC)
    as_of_1 = datetime(2026, 1, 2, tzinfo=UTC)
    as_of_2 = datetime(2026, 1, 5, tzinfo=UTC)
    await w.write(sid, [ObservationIn(ts=ts, as_of=as_of_1, value=100.0)])
    await session.commit()
    await w.write(sid, [ObservationIn(ts=ts, as_of=as_of_2, value=110.0)])
    await session.commit()

    r = ObservationReader(session)

    # PIT between as_of_1 and as_of_2 → original.
    o = await r.latest(sid, as_of=datetime(2026, 1, 3, tzinfo=UTC))
    assert o is not None
    assert o.value == 100.0
    assert o.as_of == as_of_1

    # PIT after both → restated.
    o = await r.latest(sid, as_of=datetime(2026, 1, 10, tzinfo=UTC))
    assert o is not None
    assert o.value == 110.0
    assert o.as_of == as_of_2

    # PIT before any as_of → no row visible.
    o = await r.latest(sid, as_of=datetime(2026, 1, 1, 12, tzinfo=UTC))
    assert o is None

    # No PIT → uses latest as_of (= 110.0).
    o = await r.latest(sid)
    assert o is not None
    assert o.value == 110.0


@pytest.mark.asyncio(loop_scope="session")
async def test_latest_returns_newest_ts_not_newest_restatement(
    session: AsyncSession,
) -> None:
    """Codex F17, 2026-04-29: ``latest()`` must return the highest ts
    AFTER PIT collapse, NOT the row with the highest ``as_of`` globally.

    Failure mode this catches: an implementer ordering by ``as_of DESC``
    first instead of ``ts DESC, as_of DESC``. With the seeded data, a
    wrong order-by would return the OLDER ts because its restatement
    has the latest ``as_of`` of the whole table.

    Seeded data:
      ts=2026-01-01, as_of=2026-01-02 (initial),     value=10
      ts=2026-02-01, as_of=2026-02-02 (initial),     value=20  <-- newest ts
      ts=2026-01-01, as_of=2026-03-01 (restatement), value=15  <-- newest as_of

    PIT = 2026-04-01: ``latest()`` MUST return ts=2026-02-01, value=20.
    """
    rid = await _new_run(session, job="lat-order")
    sid = await _seed_series(session, rid, code="l.order")
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)

    older_ts = datetime(2026, 1, 1, tzinfo=UTC)
    newer_ts = datetime(2026, 2, 1, tzinfo=UTC)
    # Older ts, initial observation (value=10).
    await w.write(
        sid,
        [
            ObservationIn(
                ts=older_ts,
                as_of=datetime(2026, 1, 2, tzinfo=UTC),
                value=10.0,
            ),
        ],
    )
    await session.commit()
    # Newer ts, initial observation (value=20). Lands BEFORE the
    # older-ts restatement so the table's max as_of is on the older ts.
    await w.write(
        sid,
        [
            ObservationIn(
                ts=newer_ts,
                as_of=datetime(2026, 2, 2, tzinfo=UTC),
                value=20.0,
            ),
        ],
    )
    await session.commit()
    # Older ts, restatement (value=15). Its as_of (2026-03-01) is newer
    # than every other row's as_of in the table. A wrong "ORDER BY
    # as_of DESC" would collapse to this row and return value=15.
    await w.write(
        sid,
        [
            ObservationIn(
                ts=older_ts,
                as_of=datetime(2026, 3, 1, tzinfo=UTC),
                value=15.0,
            ),
        ],
    )
    await session.commit()

    r = ObservationReader(session)
    o = await r.latest(sid, as_of=datetime(2026, 4, 1, tzinfo=UTC))
    assert o is not None
    assert o.ts == newer_ts, (
        f"latest() returned ts={o.ts.isoformat()} value={o.value}; "
        f"expected ts={newer_ts.isoformat()} value=20.0. Likely a wrong "
        f"ORDER BY: must be (ts DESC, as_of DESC), not (as_of DESC, ts DESC)."
    )
    assert o.value == 20.0


@pytest.mark.asyncio(loop_scope="session")
async def test_latest_as_of_in_past_returns_latest_known_at_that_time(
    session: AsyncSession,
) -> None:
    """``as_of=<past>`` returns the latest known fact recorded by that
    moment, ignoring any later restatements. Two ts (2026-01-01,
    2026-01-02), each with their initial as_of recorded later. Pinning
    PIT before the second initial-as_of must return the FIRST ts (the
    only one whose initial as_of was already on file).
    """
    rid = await _new_run(session, job="lat-past")
    sid = await _seed_series(session, rid, code="l.past")
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)

    ts1 = datetime(2026, 1, 1, tzinfo=UTC)
    ts2 = datetime(2026, 1, 2, tzinfo=UTC)
    await w.write(
        sid,
        [
            # ts1's initial as_of is 2026-01-10 (recorded with delay).
            ObservationIn(
                ts=ts1,
                as_of=datetime(2026, 1, 10, tzinfo=UTC),
                value=1.0,
            ),
            # ts2's initial as_of is 2026-01-20 (recorded later still).
            ObservationIn(
                ts=ts2,
                as_of=datetime(2026, 1, 20, tzinfo=UTC),
                value=2.0,
            ),
        ],
    )
    await session.commit()

    r = ObservationReader(session)
    # PIT 2026-01-15: only ts1's row was on file by then → that wins.
    o = await r.latest(sid, as_of=datetime(2026, 1, 15, tzinfo=UTC))
    assert o is not None
    assert o.ts == ts1
    assert o.value == 1.0
