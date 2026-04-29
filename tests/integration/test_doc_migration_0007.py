from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_filing_body_table(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='doc' AND table_name='filing_body'"
            )
        )
    ).all()
    cols = {r.column_name for r in rows}
    for c in ["filing_id", "body_text", "body_lang", "body_fts", "extracted_at"]:
        assert c in cols


@pytest.mark.asyncio(loop_scope="session")
async def test_filing_body_fts_gin_index(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname='doc' AND tablename='filing_body'"
            )
        )
    ).all()
    fts_indexes = [
        r for r in rows if "gin" in r.indexdef.lower() and "body_fts" in r.indexdef.lower()
    ]
    assert fts_indexes, "expected a GIN index over body_fts"
