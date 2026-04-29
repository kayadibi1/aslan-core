"""Integration tests — true two-session concurrent regression for
``ObservationWriter.upsert_series`` AND ``ObservationWriter.write``.

Plan reference:
- ``upsert_series`` race: codex 2026-04-29, v0.4.0 Batch 2 follow-up.
- ``write`` race: v0.4.0 Task 18.

Both use ``asyncio.gather`` over independent sessions on independent
connections, mirroring the v0.3 ``test_document_store_concurrency``
pattern. The sequential idempotency test in
``test_observation_writer_upsert_strict_metrics.py`` does not
exercise the actual race because both calls run on a single
connection.
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


@pytest.mark.asyncio(loop_scope="session")
async def test_two_writers_overlapping_batches_no_fork(pg_dsn: str) -> None:
    """v0.4.0 Task 18 — two writers racing on overlapping batches for
    the SAME series must produce no fork. The per-series advisory lock
    in Phase 1 serialises both writers; whoever gets the lock first
    wins the INSERT, the other sees the rows as ``unchanged`` (since
    the payloads are identical).

    Verifies:
    * neither call raises;
    * inserted_a + inserted_b == 25 (one writer inserts everything,
      the other inserts 0);
    * the DB shows exactly 25 rows for the shared (series_id);
    * one or two ``observation.write_batch`` events landed (one per
      writer call) with combined batch_size of 50 — both writers ran
      their full Phase-2 even though only one's INSERTs took effect.

    Uses two independent sessions on independent connections so the
    race is real, not sequential.
    """
    from aslan_core.audit import Actor, set_actor
    from aslan_core.schemas.timeseries import ObservationIn
    from aslan_core.timeseries.writer import ObservationWriter

    engine = create_async_engine(pg_dsn)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)

        # Seed: clean ts.* + a kap source + a fresh ingestion_run.
        async with factory() as s:
            await s.execute(text("DELETE FROM audit.observation_batch_keys"))
            await s.execute(text("DELETE FROM ts.observation"))
            await s.execute(text("DELETE FROM ts.series_subject"))
            await s.execute(text("DELETE FROM ts.series_catalog"))
            await s.execute(text("DELETE FROM audit.events WHERE target_schema = 'ts'"))
            await s.execute(
                text(
                    "INSERT INTO src.source (source_id, name, kind, license_status) "
                    "VALUES ('kap', 'KAP', 'scraper', 'open') ON CONFLICT DO NOTHING"
                )
            )
            run_id_row = await s.execute(
                text(
                    "INSERT INTO src.ingestion_run (source_id, job_name, status) "
                    "VALUES ('kap', 'write_concurrency', 'succeeded') "
                    "RETURNING ingestion_run_id"
                )
            )
            run_id: int = run_id_row.scalar_one()
            # Seed the series outside the racing writers so they only
            # contend on the per-series advisory lock during write().
            set_actor(Actor(actor_id="user:setup", actor_kind="user"))
            seeder = ObservationWriter(s, ingestion_run_id=run_id)
            sr = await seeder.upsert_series(
                series_code="con.write.1",
                source_id="kap",
                metric="m",
                frequency="1d",
                unit="TRY",
            )
            await s.commit()
            series_id = sr.series_id
            set_actor(None)

        # 25 observations with overlapping (ts, as_of) — both writers
        # send the same set; identical payloads => the loser's rows
        # all hit DO NOTHING and count as 'unchanged'.
        from datetime import UTC, datetime

        def _obs() -> list[ObservationIn]:
            return [
                ObservationIn(
                    ts=datetime(2026, 4, d, tzinfo=UTC),
                    as_of=datetime(2026, 4, d, 12, tzinfo=UTC),
                    value=float(d),
                )
                for d in range(1, 26)
            ]

        async def _writer(label: str) -> tuple[int, int]:
            async with factory() as s:
                set_actor(Actor(actor_id=f"user:{label}", actor_kind="user"))
                try:
                    local_w = ObservationWriter(s, ingestion_run_id=run_id)
                    wc = await local_w.write(series_id, _obs())
                    await s.commit()
                    return wc.inserted, wc.unchanged
                finally:
                    set_actor(None)

        results = await asyncio.gather(_writer("a"), _writer("b"))
        inserted_a, unchanged_a = results[0]
        inserted_b, unchanged_b = results[1]

        # Whichever writer got the lock first inserted all 25 rows; the
        # other wrote 0 inserts (every key already existed → unchanged).
        # Sum invariant: inserted_a + inserted_b == 25.
        assert inserted_a + inserted_b == 25, (
            f"inserted={inserted_a + inserted_b} (a={inserted_a} b={inserted_b}); "
            f"expected 25 — concurrent writers forked"
        )
        assert unchanged_a + unchanged_b == 25, (
            f"unchanged={unchanged_a + unchanged_b} (a={unchanged_a} b={unchanged_b}); "
            f"expected 25 — total per-call attempted={25 + 25}"
        )

        async with factory() as s:
            n = await s.scalar(
                text("SELECT COUNT(*) FROM ts.observation WHERE series_id = :sid"),
                {"sid": series_id},
            )
            assert n == 25, f"expected 25 unique observation rows, got {n}"

            # Two write_batch events landed — one per writer call.
            n_events = await s.scalar(
                text(
                    "SELECT COUNT(*) FROM audit.events "
                    "WHERE target_schema='ts' AND target_table='observation' "
                    "  AND ingestion_run_id = :rid"
                ),
                {"rid": run_id},
            )
            assert n_events == 2
    finally:
        await engine.dispose()
