"""Verify agg.filing_event_review_queue."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_review_queue_columns(session: AsyncSession) -> None:
    cols = {
        r[0]
        for r in (
            await session.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='agg' AND table_name='filing_event_review_queue'"
                )
            )
        ).all()
    }
    assert {
        "review_id",
        "filing_id",
        "primary_event_id",
        "primary_model",
        "primary_payload",
        "primary_confidence",
        "verifier_model",
        "verifier_payload",
        "verifier_confidence",
        "diff_summary",
        "enqueued_at",
        "resolved_at",
        "resolved_by",
        "resolution",
        "resolution_payload",
    } <= cols


async def test_review_queue_partial_index(session: AsyncSession) -> None:
    rows = (
        (
            await session.execute(
                sa.text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname='agg' AND indexname='freview_unresolved'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert "resolved_at IS NULL" in rows[0]
