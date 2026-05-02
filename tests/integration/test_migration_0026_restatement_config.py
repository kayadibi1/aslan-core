"""Migration 0026 — agg.restatement_config table."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_restatement_config_table_exists(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'agg' AND table_name = 'restatement_config' "
            "ORDER BY ordinal_position"
        )
    )
    columns = [r[0] for r in result.fetchall()]
    assert "config_id" in columns
    assert "name" in columns
    assert "cpi_series_code" in columns
    assert "method" in columns
    assert "actor_id" in columns


async def test_restatement_config_unique_name(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO agg.restatement_config "
            "(name, cpi_series_code, base_date, applies_from, method) "
            "VALUES ('test_config_0026', 'evds.cpi', '2022-01-01', '2022-01-01', 'tas29')"
        )
    )
    await session.commit()

    with pytest.raises(Exception, match=r"unique|duplicate"):
        await session.execute(
            text(
                "INSERT INTO agg.restatement_config "
                "(name, cpi_series_code, base_date, applies_from, method) "
                "VALUES ('test_config_0026', 'evds.cpi', '2023-01-01', '2023-01-01', 'other')"
            )
        )
        await session.commit()
    await session.rollback()

    await session.execute(
        text("DELETE FROM agg.restatement_config WHERE name = 'test_config_0026'")
    )
    await session.commit()


async def test_dashboard_can_select_restatement_config(session: AsyncSession) -> None:
    result = await session.execute(
        text("SELECT has_table_privilege('aslan_dashboard', 'agg.restatement_config', 'SELECT')")
    )
    assert result.scalar() is True
