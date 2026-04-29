"""Migration 0012 — ts.observation Timescale hypertable.

Verifies the table exists, is registered as a hypertable with a
90-day chunk_time_interval, has the composite PK ``(series_id, ts,
as_of)``, the value-or-value_text CHECK, and both indexes
(``observation_series_ts_as_of`` for PIT queries, ``observation_run``
for forensic-by-run lookups).

Codex F10 — value MUST be ``DOUBLE PRECISION``, never ``NUMERIC``.
The hypertable's ``chunk_time_interval`` is intentionally larger than
``audit.events`` (90d vs 7d) because observation cardinality is
several orders of magnitude higher.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _ensure_source(session: AsyncSession, source_id: str = "kap") -> None:
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES (:sid, :sid, 'scraper', 'open') ON CONFLICT DO NOTHING"
        ),
        {"sid": source_id},
    )
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_table_and_hypertable(session: AsyncSession) -> None:
    n = await session.scalar(
        text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema='ts' AND table_name='observation'"
        )
    )
    assert n == 1
    n = await session.scalar(
        text(
            "SELECT COUNT(*) FROM timescaledb_information.hypertables "
            "WHERE hypertable_schema='ts' AND hypertable_name='observation'"
        )
    )
    assert n == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_chunk_time_interval_is_90d(session: AsyncSession) -> None:
    iv = await session.scalar(
        text(
            "SELECT time_interval FROM timescaledb_information.dimensions "
            "WHERE hypertable_schema='ts' AND hypertable_name='observation'"
        )
    )
    assert iv is not None
    # asyncpg returns a Python timedelta for INTERVAL.
    assert str(iv) in ("90 days", "90 days, 0:00:00")


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_value_is_double_precision(session: AsyncSession) -> None:
    """Codex F10 — value MUST be DOUBLE PRECISION (binary64), not NUMERIC."""
    dt = await session.scalar(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='ts' AND table_name='observation' AND column_name='value'"
        )
    )
    assert dt == "double precision"


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_pk_composite(session: AsyncSession) -> None:
    rows = await session.execute(
        text(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'ts.observation'::regclass AND i.indisprimary "
            "ORDER BY array_position(i.indkey, a.attnum)"
        )
    )
    pk_cols = [r.attname for r in rows]
    assert pk_cols == ["series_id", "ts", "as_of"]


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_indexes_present(session: AsyncSession) -> None:
    rows = await session.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname='ts' AND tablename='observation'")
    )
    names = {r.indexname for r in rows}
    for expected in ("observation_series_ts_as_of", "observation_run"):
        assert expected in names, f"missing index {expected}; got {names}"


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_value_check_constraint_enforced(session: AsyncSession) -> None:
    """Inserting a row with NULL value AND NULL value_text must fail."""
    await _ensure_source(session)
    await session.execute(
        text(
            "INSERT INTO ts.series_catalog (series_code, source_id, metric, frequency, unit) "
            "VALUES ('mig12_chk', 'kap', 'm', '1d', 'TRY') ON CONFLICT DO NOTHING"
        )
    )
    sid = await session.scalar(
        text("SELECT series_id FROM ts.series_catalog WHERE series_code='mig12_chk'")
    )
    rid = await session.scalar(
        text(
            "INSERT INTO src.ingestion_run (source_id, job_name, status) "
            "VALUES ('kap', 'mig12_chk', 'succeeded') RETURNING ingestion_run_id"
        )
    )
    await session.commit()

    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO ts.observation "
                "(series_id, ts, as_of, ingestion_run_id, payload_hash) "
                "VALUES (:sid, now(), now(), :rid, repeat('a', 64))"
            ),
            {"sid": sid, "rid": rid},
        )
        await session.commit()
    await session.rollback()
    # Cleanup so the FK pin doesn't keep the test series alive across runs.
    await session.execute(text("DELETE FROM ts.series_catalog WHERE series_code='mig12_chk'"))
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_fk_to_series_is_restrict(session: AsyncSession) -> None:
    """FK to ts.series_catalog must be ON DELETE RESTRICT (not CASCADE)
    so a stray DELETE on series_catalog doesn't silently nuke history.
    """
    confdeltype = await session.scalar(
        text(
            "SELECT confdeltype FROM pg_constraint c "
            "JOIN pg_class t ON c.conrelid = t.oid "
            "JOIN pg_namespace n ON t.relnamespace = n.oid "
            "WHERE n.nspname='ts' AND t.relname='observation' "
            "AND c.contype='f' AND c.conname LIKE '%series_id%'"
        )
    )
    # 'r' = RESTRICT, 'a' = NO ACTION (default), 'c' = CASCADE
    # asyncpg returns pg_constraint.confdeltype (a "char") as a single
    # byte, so normalise to str before comparing.
    assert confdeltype is not None
    deltype = confdeltype.decode() if isinstance(confdeltype, (bytes, bytearray)) else confdeltype
    assert deltype in ("r", "a"), (
        f"observation→series_catalog FK should be RESTRICT/NO ACTION, got {deltype!r}"
    )
