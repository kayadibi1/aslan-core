"""Integration tests — restatement contract for ``ObservationWriter.write``.

Plan reference: v0.4.0 implementation plan Task 15. The spec's
signature pattern (codex F2): a correction is NEVER an overwrite of
an existing key. Restatement = a NEW row at the same ``(series_id,
ts)`` with a fresh ``as_of`` (typically ``datetime.now(UTC)``). Both
rows persist; PIT replay collapses to the right one.

If the caller forgot to bump ``as_of`` and is trying to overwrite
with a different value, that's exactly the codex F2 bug class —
ObservationConflict must fire.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import Actor, set_actor
from aslan_core.errors import ObservationConflict
from aslan_core.schemas.timeseries import ObservationIn
from aslan_core.timeseries import ObservationWriter

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


async def _seed(session: AsyncSession, job: str) -> int:
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('kap', 'KAP', 'scraper', 'open') ON CONFLICT DO NOTHING"
        )
    )
    rid = await session.scalar(
        text(
            "INSERT INTO src.ingestion_run (source_id, job_name) "
            "VALUES ('kap', :job) RETURNING ingestion_run_id"
        ),
        {"job": job},
    )
    await session.commit()
    return int(rid)


@pytest.mark.asyncio(loop_scope="session")
async def test_restatement_creates_new_row_with_fresh_as_of(
    session: AsyncSession,
) -> None:
    """Restatement = NEW (ts, as_of) key with fresh as_of. Both rows
    coexist; the original is untouched."""
    rid = await _seed(session, "rs")
    set_actor(Actor(actor_id="user:rs", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    sr = await w.upsert_series(
        series_code="rs.1",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    ts = datetime(2026, 1, 1, tzinfo=UTC)
    # Original
    await w.write(
        sr.series_id,
        [ObservationIn(ts=ts, as_of=datetime(2026, 1, 2, tzinfo=UTC), value=100.0)],
    )
    await session.commit()
    # Restatement: same ts, fresh as_of
    await w.write(
        sr.series_id,
        [ObservationIn(ts=ts, as_of=datetime(2026, 1, 5, tzinfo=UTC), value=110.0)],
    )
    await session.commit()

    rows = (
        await session.execute(
            text(
                "SELECT ts, as_of, value FROM ts.observation WHERE series_id = :sid ORDER BY as_of"
            ),
            {"sid": sr.series_id},
        )
    ).all()
    assert len(rows) == 2
    assert rows[0].as_of == datetime(2026, 1, 2, tzinfo=UTC)
    assert rows[0].value == 100.0
    assert rows[1].as_of == datetime(2026, 1, 5, tzinfo=UTC)
    assert rows[1].value == 110.0


@pytest.mark.asyncio(loop_scope="session")
async def test_restatement_with_same_as_of_different_value_raises(
    session: AsyncSession,
) -> None:
    """If the caller forgot to bump as_of and is trying to overwrite
    with a different value, that's exactly the codex-F2 bug — must
    raise ObservationConflict."""
    rid = await _seed(session, "rs2")
    set_actor(Actor(actor_id="user:rs", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    sr = await w.upsert_series(
        series_code="rs.2",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    ts = datetime(2026, 1, 1, tzinfo=UTC)
    as_of = datetime(2026, 1, 2, tzinfo=UTC)
    await w.write(sr.series_id, [ObservationIn(ts=ts, as_of=as_of, value=100.0)])
    await session.commit()
    with pytest.raises(ObservationConflict):
        await w.write(sr.series_id, [ObservationIn(ts=ts, as_of=as_of, value=110.0)])
    await session.rollback()
