"""Migration 0028 — agg.observation_daily_to_monthly continuous aggregate."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_continuous_aggregate_exists(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT view_name FROM timescaledb_information.continuous_aggregates "
            "WHERE view_schema = 'agg' "
            "AND view_name = 'observation_daily_to_monthly'"
        )
    )
    assert result.scalar() == "observation_daily_to_monthly"


async def test_continuous_aggregate_policy_exists(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT count(*) FROM timescaledb_information.jobs "
            "WHERE hypertable_schema = 'agg' "
            "AND hypertable_name = 'observation_daily_to_monthly'"
        )
    )
    count = result.scalar()
    assert count is not None
    assert count >= 1


async def test_empty_aggregate_returns_no_rows(session: AsyncSession) -> None:
    result = await session.execute(text("SELECT count(*) FROM agg.observation_daily_to_monthly"))
    assert result.scalar() == 0


async def test_dashboard_can_select_aggregate(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT has_table_privilege('aslan_dashboard', "
            "'agg.observation_daily_to_monthly', 'SELECT')"
        )
    )
    assert result.scalar() is True
