"""Verify agg.filing_event_pii_index — schema, cascade, no-grant invariant."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_pii_index_columns(session: AsyncSession) -> None:
    cols = {
        r[0]
        for r in (
            await session.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='agg' AND table_name='filing_event_pii_index'"
                )
            )
        ).all()
    }
    assert {
        "filing_event_id",
        "pii_kind",
        "payload_path",
        "redacted",
        "redacted_at",
        "redaction_reason",
    } <= cols


async def test_pii_index_fk_cascade(session: AsyncSession) -> None:
    rows = (
        (
            await session.execute(
                sa.text(
                    "SELECT confdeltype FROM pg_constraint c "
                    "JOIN pg_class cl ON cl.oid = c.conrelid "
                    "JOIN pg_namespace n ON n.oid = cl.relnamespace "
                    "WHERE cl.relname='filing_event_pii_index' "
                    "AND n.nspname='agg' AND c.contype='f'"
                )
            )
        )
        .scalars()
        .all()
    )
    # 'c' is CASCADE in pg_constraint.confdeltype. asyncpg returns char(1)
    # as bytes, so compare against b'c'.
    assert b"c" in rows or "c" in rows


async def test_pii_index_not_granted_to_dashboard(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            sa.text(
                "SELECT has_table_privilege('aslan_dashboard', "
                "'agg.filing_event_pii_index', 'SELECT')"
            )
        )
    ).scalar()
    assert rows is False, (
        "agg.filing_event_pii_index must remain ungranted to aslan_dashboard "
        "(privileged-only PII index)."
    )
