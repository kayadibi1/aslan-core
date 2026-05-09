"""Verify agg.filing_event_label."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_filing_event_label_columns(session: AsyncSession) -> None:
    cols = {
        r[0]
        for r in (
            await session.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='agg' AND table_name='filing_event_label'"
                )
            )
        ).all()
    }
    assert {
        "label_id",
        "filing_id",
        "event_type",
        "event_seq",
        "expected_payload",
        "labeller",
        "confirmed_by",
        "confidence",
        "is_holdout",
        "notes",
        "labelled_at",
    } <= cols


async def test_holdout_partial_index(session: AsyncSession) -> None:
    rows = (
        (
            await session.execute(
                sa.text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname='agg' AND indexname='fel_holdout'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert "is_holdout = true" in rows[0]
