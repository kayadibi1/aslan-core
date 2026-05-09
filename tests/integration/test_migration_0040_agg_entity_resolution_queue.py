"""Verify agg.entity_resolution_queue."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_entity_resolution_queue_columns(session: AsyncSession) -> None:
    cols = {
        r[0]
        for r in (
            await session.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='agg' AND table_name='entity_resolution_queue'"
                )
            )
        ).all()
    }
    assert {
        "queue_id",
        "source_event_id",
        "candidate_name",
        "candidate_kind",
        "candidate_country",
        "payload_path",
        "resolved_entity_id",
        "resolved_at",
        "resolved_by",
        "enqueued_at",
    } <= cols


async def test_erq_pending_partial_index(session: AsyncSession) -> None:
    rows = (
        (
            await session.execute(
                sa.text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname='agg' AND indexname='erq_pending'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert "resolved_entity_id IS NULL" in rows[0]
