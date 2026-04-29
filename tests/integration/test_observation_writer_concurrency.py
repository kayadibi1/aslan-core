"""Integration tests — true two-session concurrent regression for
``ObservationWriter.upsert_series``.

Plan reference: codex 2026-04-29, v0.4.0 Batch 2 follow-up. The
sequential idempotency test in
``test_observation_writer_upsert_strict_metrics.py`` does not exercise
the actual race because both calls run on a single connection. This
file uses ``asyncio.gather`` over independent sessions on independent
connections, mirroring the v0.3 ``test_document_store_concurrency``
pattern.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_fresh_upsert_serializes_via_on_conflict(
    pg_dsn: str,
) -> None:
    """Codex 2026-04-29: two writers racing on the same NEW series_code
    must NOT raise ``UniqueViolation`` on
    ``series_catalog_series_code_key``. The losing writer falls
    through to the idempotent path; original-creator attribution is
    preserved on the row.

    Uses two independent sessions on independent connections so the
    race is real, not sequential. The function-scoped ``session``
    fixture is one session pinned to one connection — insufficient
    for racing two ``upsert_series`` calls.

    Verifies:
    * neither call raises ``UniqueViolation`` (the fix);
    * exactly one ``created=True`` and one ``created=False`` come back;
    * both calls return the same ``series_id``;
    * the row's ``actor_id`` is the winner's;
    * the audit log carries one ``series.upsert`` (winner) and one
      ``series.idempotent_hit`` (loser).
    """
    from aslan_core.audit import Actor, set_actor
    from aslan_core.timeseries.writer import ObservationWriter

    engine = create_async_engine(pg_dsn)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)

        # Seed: clean ts.* (audit events for this series_code only) +
        # a kap source + a fresh ingestion_run. Do NOT wipe
        # src.ingestion_run wholesale — earlier tests in the session
        # leave doc.filing rows that FK into it.
        async with factory() as s:
            await s.execute(text("DELETE FROM ts.observation"))
            await s.execute(text("DELETE FROM ts.series_subject"))
            await s.execute(text("DELETE FROM ts.series_catalog"))
            await s.execute(
                text(
                    "DELETE FROM audit.events "
                    "WHERE target_schema = 'ts' AND target_table = 'series_catalog'"
                )
            )
            await s.execute(
                text(
                    "INSERT INTO src.source (source_id, name, kind, license_status) "
                    "VALUES ('kap', 'KAP', 'scraper', 'open') ON CONFLICT DO NOTHING"
                )
            )
            run_id_row = await s.execute(
                text(
                    "INSERT INTO src.ingestion_run (source_id, job_name, status) "
                    "VALUES ('kap', 'concurrency_test', 'succeeded') "
                    "RETURNING ingestion_run_id"
                )
            )
            run_id: int = run_id_row.scalar_one()
            await s.commit()

        async def _writer(actor_id: str) -> tuple[bool, int]:
            async with factory() as s:
                set_actor(Actor(actor_id=actor_id, actor_kind="user"))
                try:
                    w = ObservationWriter(s, ingestion_run_id=run_id)
                    result = await w.upsert_series(
                        series_code="kap.race.test",
                        source_id="kap",
                        metric="x",
                        frequency="1d",
                        unit="USD",
                    )
                    await s.commit()
                    return result.created, result.series_id
                finally:
                    set_actor(None)

        results = await asyncio.gather(
            _writer("user:racer-A"),
            _writer("user:racer-B"),
        )

        # Exactly one is created=True, the other is created=False.
        creates = sorted(r[0] for r in results)
        assert creates == [False, True]

        # Both return the same series_id (the winner's row).
        assert results[0][1] == results[1][1]

        async with factory() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT actor_id FROM ts.series_catalog WHERE series_code = 'kap.race.test'"
                    )
                )
            ).one()
            # Whichever actor won — the row's actor_id is stable: the
            # loser did NOT rewrite the winner's attribution.
            assert row.actor_id in ("user:racer-A", "user:racer-B")

            # Audit log: exactly one series.upsert + one
            # series.idempotent_hit, attributed correctly.
            events = (
                await s.execute(
                    text(
                        "SELECT operation, actor_id FROM audit.events "
                        "WHERE target_schema = 'ts' "
                        "  AND target_table = 'series_catalog' "
                        "ORDER BY occurred_at"
                    )
                )
            ).all()
            ops = sorted(e.operation for e in events)
            assert ops == ["series.idempotent_hit", "series.upsert"]

            upsert_event = next(e for e in events if e.operation == "series.upsert")
            hit_event = next(e for e in events if e.operation == "series.idempotent_hit")
            # Winner's audit event matches the row's actor.
            assert upsert_event.actor_id == row.actor_id
            # Loser's audit event is the OTHER actor.
            assert hit_event.actor_id != row.actor_id
            assert hit_event.actor_id in ("user:racer-A", "user:racer-B")
    finally:
        await engine.dispose()
