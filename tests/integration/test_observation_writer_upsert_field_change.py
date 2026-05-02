"""Integration tests — ``ObservationWriter.upsert_series`` field-change path.

Plan reference: v0.4.0 Task 10. Field-change path: ``series_code``
exists, but at least one passed field differs from the existing row.

Behaviour:

* UPDATE the row's mutable fields, bump ``updated_at = now()``, and
  restamp audit columns to the current actor (last-writer-wins on the
  row's denormalised audit cols, consistent with v0.3 case-B for
  filings).
* Emit a ``series.update`` audit event with full ``before`` + ``after``
  payloads.
* If observations exist for the series AND any of
  ``source_id`` / ``frequency`` / ``unit`` would change, raise
  :class:`SeriesCodeConflict` instead — those fields are immutable
  once data is recorded because changing them silently breaks the
  time-series semantics readers rely on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import Actor, set_actor
from aslan_core.errors import SeriesCodeConflict
from aslan_core.timeseries import ObservationWriter

pytestmark = pytest.mark.integration


async def _seed_sources(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('kap', 'KAP', 'scraper', 'open') "
            "ON CONFLICT DO NOTHING"
        )
    )
    await session.commit()


async def _new_run(session: AsyncSession) -> int:
    await _seed_sources(session)
    rid = await session.scalar(
        text(
            "INSERT INTO src.ingestion_run (source_id, job_name) "
            "VALUES ('kap', 'test') RETURNING ingestion_run_id"
        )
    )
    await session.commit()
    return int(rid)


async def _wipe(session: AsyncSession) -> None:
    for stmt in [
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


@pytest.mark.asyncio(loop_scope="session")
async def test_upsert_series_field_change_updates_and_emits_series_update(
    session: AsyncSession,
) -> None:
    await _wipe(session)
    rid = await _new_run(session)

    set_actor(Actor(actor_id="user:original", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    r = await w.upsert_series(
        series_code="fc.1",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
        description="initial",
    )
    await session.commit()

    set_actor(Actor(actor_id="user:editor", actor_kind="user"))
    r2 = await w.upsert_series(
        series_code="fc.1",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
        description="updated description",
    )
    await session.commit()

    assert r.series_id == r2.series_id
    assert r2.created is False

    # The row's description AND audit cols are updated.
    row = (
        await session.execute(
            text(
                "SELECT description, actor_id, updated_at "
                "FROM ts.series_catalog WHERE series_id = :sid"
            ),
            {"sid": r.series_id},
        )
    ).one()
    assert row.description == "updated description"
    assert row.actor_id == "user:editor"

    # The audit log shows series.upsert + series.update.
    rows = (
        await session.execute(
            text(
                "SELECT operation, before, after FROM audit.events "
                "WHERE target_schema='ts' AND target_table='series_catalog' "
                "ORDER BY occurred_at"
            )
        )
    ).all()
    ops = [r.operation for r in rows]
    assert ops == ["series.upsert", "series.update"]
    upd = rows[1]
    assert upd.before["description"] == "initial"
    assert upd.after["description"] == "updated description"


@pytest.mark.asyncio(loop_scope="session")
async def test_field_change_blocked_when_observations_exist_and_frequency_changes(
    session: AsyncSession,
) -> None:
    """Changing frequency on a series that already has observations
    breaks time-series semantics — must raise SeriesCodeConflict."""
    await _wipe(session)
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)

    r = await w.upsert_series(
        series_code="fc.lock",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    # Insert one observation so the immutable-field guard fires.
    await session.execute(
        text(
            "INSERT INTO ts.observation "
            "(series_id, ts, as_of, value, ingestion_run_id, payload_hash) "
            "VALUES (:sid, '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00', "
            "        1.0, :rid, repeat('a', 64))"
        ),
        {"sid": r.series_id, "rid": rid},
    )
    await session.commit()

    with pytest.raises(SeriesCodeConflict, match="frequency"):
        await w.upsert_series(
            series_code="fc.lock",
            source_id="kap",
            metric="m",
            frequency="1h",  # changed!
            unit="TRY",
        )
