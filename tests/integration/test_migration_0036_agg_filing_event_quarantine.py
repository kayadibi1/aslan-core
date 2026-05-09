"""Verify agg.filing_event_quarantine table."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_quarantine_table_columns(session: AsyncSession) -> None:
    cols = {
        r[0]
        for r in (
            await session.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='agg' AND table_name='filing_event_quarantine'"
                )
            )
        ).all()
    }
    assert {
        "filing_id",
        "reason",
        "detail",
        "last_attempt",
        "attempt_count",
        "quarantined_at",
    } <= cols
