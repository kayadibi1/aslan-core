"""Migration 0027 — agg.entity_latest_snapshot materialized view."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_entity_latest_snapshot_exists(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT matviewname FROM pg_matviews "
            "WHERE schemaname = 'agg' AND matviewname = 'entity_latest_snapshot'"
        )
    )
    assert result.scalar() == "entity_latest_snapshot"


async def test_entity_latest_snapshot_has_unique_index(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'agg' AND tablename = 'entity_latest_snapshot' "
            "AND indexname = 'entity_latest_snapshot_pk'"
        )
    )
    assert result.scalar() == "entity_latest_snapshot_pk"


async def test_concurrent_refresh_works(session: AsyncSession) -> None:
    await session.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY agg.entity_latest_snapshot"))
    result = await session.execute(text("SELECT count(*) FROM agg.entity_latest_snapshot"))
    count = result.scalar()
    entity_count = (
        await session.execute(text("SELECT count(*) FROM ref.entity WHERE status = 'active'"))
    ).scalar()
    assert count == entity_count


async def test_dashboard_can_select_snapshot(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT has_table_privilege('aslan_dashboard', 'agg.entity_latest_snapshot', 'SELECT')"
        )
    )
    assert result.scalar() is True
