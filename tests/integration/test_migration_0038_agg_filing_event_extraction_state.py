"""Verify agg.filing_event_extraction_state."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_extraction_state_pk(session: AsyncSession) -> None:
    rows = (
        (
            await session.execute(
                sa.text(
                    """
                SELECT a.attname FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                WHERE i.indisprimary
                  AND i.indrelid = 'agg.filing_event_extraction_state'::regclass
                ORDER BY array_position(i.indkey, a.attnum)
                """
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == ["filing_id", "model_version", "prompt_version"]


async def test_extraction_state_partial_index(session: AsyncSession) -> None:
    rows = (
        (
            await session.execute(
                sa.text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname='agg' AND indexname='fes_pending'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert "status = 'pending'" in rows[0]
