from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_filing_attachment_columns(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='doc' AND table_name='filing_attachment'"
            )
        )
    ).all()
    cols = {r.column_name for r in rows}
    for c in [
        "attachment_id",
        "filing_id",
        "object_key",
        "mime",
        "sha256",
        "bytes",
        "role",
        "sequence",
        "created_at",
    ]:
        assert c in cols


@pytest.mark.asyncio(loop_scope="session")
async def test_filing_attachment_idempotency_constraint(session: AsyncSession) -> None:
    """UNIQUE (filing_id, sha256)."""
    rows = (
        await session.execute(
            text(
                "SELECT array_agg(att.attname ORDER BY u.attnum) AS cols "
                "FROM pg_constraint c "
                "JOIN pg_class t ON c.conrelid = t.oid "
                "JOIN pg_namespace n ON t.relnamespace = n.oid "
                "CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS u(attnum, ord) "
                "JOIN pg_attribute att ON att.attrelid = t.oid AND att.attnum = u.attnum "
                "WHERE n.nspname = 'doc' AND t.relname = 'filing_attachment' AND c.contype = 'u' "
                "GROUP BY c.conname"
            )
        )
    ).all()
    cols_sets = {tuple(r.cols) for r in rows}
    assert ("filing_id", "sha256") in cols_sets
