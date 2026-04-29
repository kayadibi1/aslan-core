"""Integration tests — ``ObservationReader.range``.

Plan reference: v0.4.0 Task 22. DISTINCT ON PIT collapse over a
half-open ``[ts_start, ts_end)`` window:

    SELECT DISTINCT ON (series_id, ts) *
    FROM ts.observation
    WHERE series_id = :sid
      AND ts >= :ts_start AND ts < :ts_end
      AND (:pit IS NULL OR as_of <= :pit)
    ORDER BY series_id, ts ASC, as_of DESC
    [LIMIT :limit]

For each ``(series_id, ts)``, only the row with max ``as_of <= pit``
survives. Restated rows must NOT inflate the result — at most one row
per timestamp.
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
async def test_range_excludes_ts_end(session: AsyncSession) -> None:
    """``[ts_start, ts_end)`` — half-open. ``ts_end`` is exclusive."""
    rid = await _new_run(session, job="rng-bounds")
    sid = await _seed_series(session, rid, code="rng.bounds")
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
    rows = await r.range(
        sid,
        datetime(2026, 1, 2, tzinfo=UTC),
        datetime(2026, 1, 5, tzinfo=UTC),
    )
    assert [o.ts.day for o in rows] == [2, 3, 4]


@pytest.mark.asyncio(loop_scope="session")
async def test_range_includes_ts_start(session: AsyncSession) -> None:
    """``ts_start`` is inclusive."""
    rid = await _new_run(session, job="rng-start")
    sid = await _seed_series(session, rid, code="rng.start")
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
            for d in range(1, 4)
        ],
    )
    await session.commit()

    r = ObservationReader(session)
    rows = await r.range(
        sid,
        datetime(2026, 1, 1, tzinfo=UTC),  # inclusive
        datetime(2026, 1, 4, tzinfo=UTC),
    )
    assert [o.ts.day for o in rows] == [1, 2, 3]


@pytest.mark.asyncio(loop_scope="session")
async def test_range_pit_collapse_keeps_one_row_per_ts(
    session: AsyncSession,
) -> None:
    """Codex F1 — restated rows must NOT inflate the result. After PIT
    collapse, exactly one row per ts. Two ``as_of`` for same ts:
    PIT-after returns the restatement; PIT-between returns the original.
    """
    rid = await _new_run(session, job="rng-pit")
    sid = await _seed_series(session, rid, code="rng.pit")
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)

    ts = datetime(2026, 1, 1, tzinfo=UTC)
    await w.write(
        sid,
        [ObservationIn(ts=ts, as_of=datetime(2026, 1, 2, tzinfo=UTC), value=100.0)],
    )
    await session.commit()
    await w.write(
        sid,
        [ObservationIn(ts=ts, as_of=datetime(2026, 1, 5, tzinfo=UTC), value=110.0)],
    )
    await session.commit()

    r = ObservationReader(session)

    # PIT after restatement → ONE row, restated value.
    rows = await r.range(
        sid,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
        as_of=datetime(2026, 1, 10, tzinfo=UTC),
    )
    assert len(rows) == 1
    assert rows[0].value == 110.0

    # PIT between → ONE row, original value.
    rows = await r.range(
        sid,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )
    assert len(rows) == 1
    assert rows[0].value == 100.0

    # PIT before any as_of → empty.
    rows = await r.range(
        sid,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
        as_of=datetime(2026, 1, 1, 12, tzinfo=UTC),
    )
    assert rows == []


@pytest.mark.asyncio(loop_scope="session")
async def test_range_no_pit_returns_latest_as_of_per_ts(
    session: AsyncSession,
) -> None:
    """``as_of=None`` returns the latest known fact per ts (no
    restated-row inflation)."""
    rid = await _new_run(session, job="rng-no-pit")
    sid = await _seed_series(session, rid, code="rng.nopit")
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)

    ts = datetime(2026, 1, 1, tzinfo=UTC)
    await w.write(
        sid,
        [ObservationIn(ts=ts, as_of=datetime(2026, 1, 2, tzinfo=UTC), value=100.0)],
    )
    await session.commit()
    await w.write(
        sid,
        [ObservationIn(ts=ts, as_of=datetime(2026, 1, 5, tzinfo=UTC), value=110.0)],
    )
    await session.commit()

    r = ObservationReader(session)
    rows = await r.range(
        sid,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert len(rows) == 1
    assert rows[0].value == 110.0


@pytest.mark.asyncio(loop_scope="session")
async def test_range_returns_ascending_ts_order(session: AsyncSession) -> None:
    """Outer ``ORDER BY ts ASC`` — caller iterates in time order."""
    rid = await _new_run(session, job="rng-asc")
    sid = await _seed_series(session, rid, code="rng.asc")
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    # Insert OUT of order so any accidental "natural ordering" failure
    # surfaces.
    await w.write(
        sid,
        [
            ObservationIn(
                ts=datetime(2026, 1, 5, tzinfo=UTC),
                as_of=datetime(2026, 1, 6, tzinfo=UTC),
                value=5.0,
            ),
            ObservationIn(
                ts=datetime(2026, 1, 1, tzinfo=UTC),
                as_of=datetime(2026, 1, 2, tzinfo=UTC),
                value=1.0,
            ),
            ObservationIn(
                ts=datetime(2026, 1, 3, tzinfo=UTC),
                as_of=datetime(2026, 1, 4, tzinfo=UTC),
                value=3.0,
            ),
        ],
    )
    await session.commit()

    r = ObservationReader(session)
    rows = await r.range(
        sid,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 10, tzinfo=UTC),
    )
    assert [o.ts.day for o in rows] == [1, 3, 5]


@pytest.mark.asyncio(loop_scope="session")
async def test_range_limit_caps_result(session: AsyncSession) -> None:
    """``limit`` caps the post-collapse row count; ``None`` returns
    every row in the window."""
    rid = await _new_run(session, job="rng-lim")
    sid = await _seed_series(session, rid, code="rng.lim")
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
            for d in range(1, 11)
        ],
    )
    await session.commit()

    r = ObservationReader(session)
    rows = await r.range(
        sid,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 11, tzinfo=UTC),
        limit=3,
    )
    assert len(rows) == 3
    # Limit applies AFTER ascending ts ordering — earliest 3 ts come back.
    assert [o.ts.day for o in rows] == [1, 2, 3]

    # No limit → all 10 rows.
    rows_all = await r.range(
        sid,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 11, tzinfo=UTC),
    )
    assert len(rows_all) == 10


@pytest.mark.asyncio(loop_scope="session")
async def test_range_pit_filters_per_ts_winning_as_of(
    session: AsyncSession,
) -> None:
    """A row whose own winning ``as_of`` exceeds the PIT must be
    excluded — not just collapsed.

    Two ts in the window, only one with an as_of <= PIT. The other ts
    is hidden completely. Catches an implementer that filters after
    DISTINCT ON instead of inside the WHERE.
    """
    rid = await _new_run(session, job="rng-perts")
    sid = await _seed_series(session, rid, code="rng.perts")
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)

    # ts A: as_of 2026-01-02
    # ts B: as_of 2026-01-20  (later than the PIT below)
    await w.write(
        sid,
        [
            ObservationIn(
                ts=datetime(2026, 1, 1, tzinfo=UTC),
                as_of=datetime(2026, 1, 2, tzinfo=UTC),
                value=1.0,
            ),
            ObservationIn(
                ts=datetime(2026, 1, 5, tzinfo=UTC),
                as_of=datetime(2026, 1, 20, tzinfo=UTC),
                value=5.0,
            ),
        ],
    )
    await session.commit()

    r = ObservationReader(session)
    rows = await r.range(
        sid,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 10, tzinfo=UTC),
        as_of=datetime(2026, 1, 10, tzinfo=UTC),
    )
    # ts B's as_of (2026-01-20) > PIT (2026-01-10) → filtered out.
    assert len(rows) == 1
    assert rows[0].ts == datetime(2026, 1, 1, tzinfo=UTC)
    assert rows[0].value == 1.0
