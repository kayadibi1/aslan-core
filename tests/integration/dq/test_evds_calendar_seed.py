"""Verify the audit.evds_release_calendar seed from migration 0055."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def test_evds_calendar_has_seed_rows(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM audit.evds_release_calendar"))).scalar()
    assert n is not None and n >= 1


async def test_evds_calendar_grace_default(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT grace_seconds FROM audit.evds_release_calendar LIMIT 5")
            )
        ).all()
    for row in rows:
        assert row.grace_seconds == 14400
