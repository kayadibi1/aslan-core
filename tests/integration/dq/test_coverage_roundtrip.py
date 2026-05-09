"""Roundtrip test for dq.coverage.snapshot()."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aslan_core.dq import coverage

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def test_snapshot_writes_row(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    observed_at = datetime(2026, 5, 9, 10, 0, 0, tzinfo=UTC).isoformat()
    async with session_factory() as session:
        snapshot_id = await coverage.snapshot(
            session=session,
            source="bist",
            dimension="entity",
            observed_at=observed_at,
            expected_count=502,
            actual_count=500,
            missing_ids={"entities": ["TICKER1", "TICKER2"]},
            target_pct=100.0,
        )
        await session.commit()

    assert snapshot_id is not None
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT source, dimension, expected_count, actual_count, "
                    "       missing_ids, coverage_pct "
                    "FROM audit.coverage_snapshot WHERE snapshot_id = :sid"
                ),
                {"sid": snapshot_id},
            )
        ).one()
    assert row.source == "bist"
    assert row.dimension == "entity"
    assert row.expected_count == 502
    assert row.actual_count == 500
    assert row.missing_ids == {"entities": ["TICKER1", "TICKER2"]}
    assert float(row.coverage_pct) == pytest.approx(99.60)


async def test_snapshot_unique_per_observation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Re-inserting the same (source, dimension, observed_at) is
    a no-op (ON CONFLICT DO NOTHING)."""
    observed_at = datetime(2026, 5, 9, 11, 0, 0, tzinfo=UTC).isoformat()
    async with session_factory() as session:
        first = await coverage.snapshot(
            session=session,
            source="kap",
            dimension="filings_today",
            observed_at=observed_at,
            expected_count=100,
            actual_count=100,
        )
        second = await coverage.snapshot(
            session=session,
            source="kap",
            dimension="filings_today",
            observed_at=observed_at,
            expected_count=200,
            actual_count=180,
        )
        await session.commit()
    assert first is not None
    assert second is None  # ON CONFLICT swallowed; no new row
