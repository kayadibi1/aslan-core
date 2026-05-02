"""Integration tests — ``ObservationWriter.upsert_series`` idempotent path.

Plan reference: v0.4.0 Task 9. The codex F1 contract: an idempotent
retry by a different actor MUST NOT mutate the row's ``actor_id``;
attribution stays with the original creator. The retry call still
emits a ``series.idempotent_hit`` audit event with the new actor in the
event row, so forensics see "user X retried at time Z".
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import Actor, set_actor
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
async def test_upsert_series_idempotent_returns_existing_id(
    session: AsyncSession,
) -> None:
    await _wipe(session)
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:original", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)

    r1 = await w.upsert_series(
        series_code="idem.1",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    set_actor(Actor(actor_id="user:retrier", actor_kind="user"))
    w2 = ObservationWriter(session, ingestion_run_id=rid)
    r2 = await w2.upsert_series(
        series_code="idem.1",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    assert r1.series_id == r2.series_id
    assert r1.created is True
    assert r2.created is False


@pytest.mark.asyncio(loop_scope="session")
async def test_idempotent_preserves_original_actor_attribution(
    session: AsyncSession,
) -> None:
    """Codex F1, carried from v0.3: the canonical row's actor_id stays
    with the original creator even after an idempotent retry."""
    await _wipe(session)
    rid = await _new_run(session)

    set_actor(Actor(actor_id="user:original", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    r1 = await w.upsert_series(
        series_code="idem.attrib",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    set_actor(Actor(actor_id="user:retrier", actor_kind="user"))
    await w.upsert_series(
        series_code="idem.attrib",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    row = (
        await session.execute(
            text("SELECT actor_id FROM ts.series_catalog WHERE series_id = :sid"),
            {"sid": r1.series_id},
        )
    ).one()
    assert row.actor_id == "user:original"


@pytest.mark.asyncio(loop_scope="session")
async def test_idempotent_emits_idempotent_hit_event(
    session: AsyncSession,
) -> None:
    await _wipe(session)
    rid = await _new_run(session)

    set_actor(Actor(actor_id="user:original", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    await w.upsert_series(
        series_code="idem.evt",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    set_actor(Actor(actor_id="user:retrier", actor_kind="user"))
    await w.upsert_series(
        series_code="idem.evt",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    rows = (
        await session.execute(
            text(
                "SELECT operation, actor_id FROM audit.events "
                "WHERE target_schema='ts' AND target_table='series_catalog' "
                "ORDER BY occurred_at"
            )
        )
    ).all()
    ops = [r.operation for r in rows]
    assert ops == ["series.upsert", "series.idempotent_hit"]
    assert rows[0].actor_id == "user:original"
    assert rows[1].actor_id == "user:retrier"
