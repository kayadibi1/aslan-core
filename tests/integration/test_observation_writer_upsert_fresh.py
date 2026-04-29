"""Integration tests — ``ObservationWriter.upsert_series`` fresh path.

Plan reference: v0.4.0 Task 8. Verifies the single-round-trip INSERT
pattern (audit cols stamped IN the statement, no post-write UPDATE) and
the ``series.upsert`` audit event with ``before=None``.
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
            "VALUES ('kap', 'KAP', 'scraper', 'open'), "
            "       ('tcmb', 'TCMB', 'scraper', 'open') "
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
    """Wipe ts.* + audit.events so this test file does not leave
    committed ts.* rows behind that would block adjacent test files'
    ``DELETE FROM src.source`` (FK ``series_catalog_source_id_fkey``).

    Does NOT delete from ``src.ingestion_run`` / ``src.source`` —
    other tests' ``_wipe`` owns those and have FKs from ``ref.*`` /
    ``doc.*`` tables that this test file does not touch."""
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
    """Ensure committed ts.* data does not leak into adjacent tests."""
    yield
    # If the test body left the session in an aborted transaction state
    # (e.g. raised mid-write), roll back first so the cleanup DELETEs
    # can run.
    await session.rollback()
    await _wipe(session)


@pytest.mark.asyncio(loop_scope="session")
async def test_upsert_series_fresh_path_inserts_row_with_audit(
    session: AsyncSession,
) -> None:
    await _wipe(session)
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:fresh", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)

    result = await w.upsert_series(
        series_code="tcmb.policy_rate.daily",
        source_id="tcmb",
        metric="policy_rate",
        frequency="1d",
        unit="%",
        description="One-week repo rate",
    )
    await session.commit()

    assert result.series_id > 0
    assert result.created is True

    row = (
        await session.execute(
            text(
                "SELECT series_code, source_id, metric, frequency, unit, "
                "       pii_class, restatement_basis, actor_id, actor_kind "
                "FROM ts.series_catalog WHERE series_id = :sid"
            ),
            {"sid": result.series_id},
        )
    ).one()
    assert row.series_code == "tcmb.policy_rate.daily"
    assert row.actor_id == "user:fresh"
    assert row.actor_kind == "user"
    assert row.pii_class == "none"
    assert row.restatement_basis == "nominal"


@pytest.mark.asyncio(loop_scope="session")
async def test_upsert_series_fresh_emits_series_upsert_audit_event(
    session: AsyncSession,
) -> None:
    await _wipe(session)
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:audit-fresh", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)

    result = await w.upsert_series(
        series_code="audit.fresh",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    rows = (
        await session.execute(
            text(
                "SELECT operation, target_table, actor_id, target_pk, before, after "
                "FROM audit.events WHERE target_schema = 'ts'"
            )
        )
    ).all()
    assert len(rows) == 1
    e = rows[0]
    assert e.operation == "series.upsert"
    assert e.target_table == "series_catalog"
    assert e.actor_id == "user:audit-fresh"
    assert e.target_pk == {"series_id": result.series_id}
    assert e.before is None
    assert e.after["series_code"] == "audit.fresh"
    assert e.after["frequency"] == "1d"
