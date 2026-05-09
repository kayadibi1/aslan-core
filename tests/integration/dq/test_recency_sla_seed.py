"""Verify the audit.recency_sla seed from migration 0054 matches §6.0."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def test_recency_sla_six_seed_rows_present(engine: AsyncEngine) -> None:
    expected = {
        ("kap", "publish_to_db"): 300,
        ("kap", "publish_to_db_high_priority"): 60,
        ("evds", "release_window"): 14400,
        ("bist", "trade_close_to_ohlcv"): 3600,
        ("tefas", "per_fund_cadence"): 172800,
        ("mkk", "event_at_to_db"): 86400,
    }
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT source, dimension, sla_seconds FROM audit.recency_sla"))
        ).all()
    actual = {(r.source, r.dimension): r.sla_seconds for r in rows}
    assert actual == expected
