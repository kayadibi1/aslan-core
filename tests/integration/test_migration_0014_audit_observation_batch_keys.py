"""Migration 0014 — audit.observation_batch_keys hypertable (codex F3).

Per-key forensic detail for bulk observation writes — keeps
``audit.events`` low-cardinality (one row per write_batch call) while
preserving full key-level traceability. Same ``chunk_time_interval=7d``
as ``audit.events`` so retention drops in lockstep (codex F6). Not
FK-linked (Postgres FKs on Timescale composite-PK hypertables are
awkward); lifecycle alignment is by chunk interval + same-tx
atomicity inside ``ObservationWriter.write``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_obkeys_table_and_hypertable(session: AsyncSession) -> None:
    n = await session.scalar(
        text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema='audit' AND table_name='observation_batch_keys'"
        )
    )
    assert n == 1
    n = await session.scalar(
        text(
            "SELECT COUNT(*) FROM timescaledb_information.hypertables "
            "WHERE hypertable_schema='audit' "
            "AND hypertable_name='observation_batch_keys'"
        )
    )
    assert n == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_obkeys_chunk_interval_matches_events(session: AsyncSession) -> None:
    """Codex F6 — both audit.events and audit.observation_batch_keys
    use the same chunk_time_interval (7d) so retention drops in
    lockstep."""
    events_iv = await session.scalar(
        text(
            "SELECT time_interval FROM timescaledb_information.dimensions "
            "WHERE hypertable_schema='audit' AND hypertable_name='events'"
        )
    )
    obkeys_iv = await session.scalar(
        text(
            "SELECT time_interval FROM timescaledb_information.dimensions "
            "WHERE hypertable_schema='audit' AND hypertable_name='observation_batch_keys'"
        )
    )
    assert events_iv == obkeys_iv


@pytest.mark.asyncio(loop_scope="session")
async def test_obkeys_pk(session: AsyncSession) -> None:
    rows = await session.execute(
        text(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'audit.observation_batch_keys'::regclass "
            "AND i.indisprimary "
            "ORDER BY array_position(i.indkey, a.attnum)"
        )
    )
    pk_cols = [r.attname for r in rows]
    assert pk_cols == ["event_id", "occurred_at", "series_id", "ts", "as_of"]


@pytest.mark.asyncio(loop_scope="session")
async def test_action_check_rejects_unknown(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO audit.observation_batch_keys "
                "(event_id, occurred_at, series_id, ts, as_of, payload_hash, action) "
                "VALUES (1, now(), 1, now(), now(), repeat('a', 64), 'updated')"
            )
        )
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_payload_hash_must_be_64_chars(session: AsyncSession) -> None:
    """payload_hash is a CHAR(64) — short or long values are rejected."""
    with pytest.raises(Exception):  # noqa: B017 -- CHAR(N) raises StringDataRightTruncation
        await session.execute(
            text(
                "INSERT INTO audit.observation_batch_keys "
                "(event_id, occurred_at, series_id, ts, as_of, payload_hash, action) "
                "VALUES (2, now(), 1, now(), now(), 'short', 'inserted')"
            )
        )
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_obkeys_event_index_present(session: AsyncSession) -> None:
    rows = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='audit' AND tablename='observation_batch_keys'"
        )
    )
    names = {r.indexname for r in rows}
    assert "obkeys_event" in names


@pytest.mark.asyncio(loop_scope="session")
async def test_obkeys_inserted_action_is_accepted(session: AsyncSession) -> None:
    """Sanity — the 'inserted' and 'unchanged' actions are accepted."""
    await session.execute(
        text(
            "INSERT INTO audit.observation_batch_keys "
            "(event_id, occurred_at, series_id, ts, as_of, payload_hash, action) "
            "VALUES (10001, now(), 1, now(), now(), repeat('a', 64), 'inserted'), "
            "       (10002, now(), 1, now(), now(), repeat('b', 64), 'unchanged')"
        )
    )
    await session.commit()
    await session.execute(
        text("DELETE FROM audit.observation_batch_keys WHERE event_id IN (10001, 10002)")
    )
    await session.commit()
