from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_filing_table_required_columns(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'doc' AND table_name = 'filing' "
                "ORDER BY ordinal_position"
            )
        )
    ).all()
    cols = {r.column_name: r for r in rows}
    for required in [
        "filing_id",
        "source_id",
        "source_filing_ref",
        "entity_id",
        "kind",
        "subkind",
        "title",
        "language",
        "published_at",
        "period_start",
        "period_end",
        "source_url",
        "is_amendment",
        "previous_filing_id",
        "primary_object_key",
        "primary_mime",
        "primary_sha256",
        "primary_bytes",
        "extracted_text_key",
        "has_xbrl",
        "xbrl_object_key",
        "metadata",
        "ingestion_run_id",
        "discovered_at",
        "revision_no",
    ]:
        assert required in cols, f"missing doc.filing.{required}"
    # revision_no is NOT NULL int
    assert cols["revision_no"].is_nullable == "NO"


@pytest.mark.asyncio(loop_scope="session")
async def test_filing_unique_constraints(session: AsyncSession) -> None:
    """Both UNIQUE constraints exist: hash dedup AND single-head per source_ref."""
    rows = (
        await session.execute(
            text(
                "SELECT conname, "
                "       array_agg(att.attname ORDER BY u.attnum) AS cols "
                "FROM pg_constraint c "
                "JOIN pg_class t ON c.conrelid = t.oid "
                "JOIN pg_namespace n ON t.relnamespace = n.oid "
                "CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS u(attnum, ord) "
                "JOIN pg_attribute att ON att.attrelid = t.oid AND att.attnum = u.attnum "
                "WHERE n.nspname = 'doc' AND t.relname = 'filing' AND c.contype = 'u' "
                "GROUP BY c.conname"
            )
        )
    ).all()
    cols_sets = {tuple(r.cols) for r in rows}
    assert (
        "source_id",
        "primary_sha256",
    ) in cols_sets, "expected UNIQUE (source_id, primary_sha256) for hash dedup"
    assert ("source_id", "source_filing_ref", "revision_no") in cols_sets, (
        "expected UNIQUE (source_id, source_filing_ref, revision_no) for single-head invariant"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_filing_lookup_index_exists(session: AsyncSession) -> None:
    """Composite index for find_by_source_ref's latest-revision query."""
    rows = (
        await session.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'doc' AND tablename = 'filing'"
            )
        )
    ).all()
    names = {r.indexname for r in rows}
    assert any("source_ref" in n for n in names), (
        "expected an index covering (source_id, source_filing_ref, revision_no DESC)"
    )
