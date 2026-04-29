from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_put_filing_serializes_via_advisory_lock(
    pg_dsn: str,
) -> None:
    """Two concurrent put_filing calls on the same source_ref + DIFFERENT
    bytes serialize via advisory lock; revision_no advances monotonically;
    no fork (no two terminal heads sharing revision_no=1).

    Verifies the codex-flagged 2026-04-28 fix. Without the per-source-ref
    advisory lock, both writers could read zero prior rows and both insert
    revision_no=1 — sibling revisions, ambiguous find_by_source_ref. The
    lock prevents this.

    Uses its own engine + sessionmaker because the two writers run via
    asyncio.gather and need independent sessions on independent
    connections — the function-scoped `session` fixture is one session
    pinned to one connection.
    """
    from aslan_core.documents.client import DocumentStore
    from aslan_core.documents.object_storage import InMemoryFake

    engine = create_async_engine(pg_dsn)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)

        # Seed a clean filings table + the kap source + an ingestion_run.
        async with factory() as s:
            await s.execute(text("DELETE FROM doc.filing_body"))
            await s.execute(text("DELETE FROM doc.filing_attachment"))
            await s.execute(text("DELETE FROM doc.filing"))
            await s.execute(text("DELETE FROM src.ingestion_run"))
            await s.execute(
                text(
                    "INSERT INTO src.source (source_id, name, kind, license_status) "
                    "VALUES ('kap', 'KAP', 'scraper', 'open') ON CONFLICT DO NOTHING"
                )
            )
            run_id_row = await s.execute(
                text(
                    "INSERT INTO src.ingestion_run (source_id, job_name, status) "
                    "VALUES ('kap', 'concurrency_test', 'succeeded') RETURNING ingestion_run_id"
                )
            )
            run_id: int = run_id_row.scalar_one()
            await s.commit()

        fake = InMemoryFake()  # shared across both tasks

        async def _writer(body: bytes) -> int:
            async with factory() as s:
                store = DocumentStore(s, object_client=fake, ingestion_run_id=run_id)
                r = await store.put_filing(
                    source_id="kap",
                    source_filing_ref="CONCURRENT",
                    entity_id=None,
                    kind="news",
                    title="t",
                    published_at=datetime(2026, 4, 28, tzinfo=UTC),
                    primary_bytes=body,
                    primary_mime="text/html",
                    primary_filename="main.html",
                )
                await s.commit()
                return r.revision_no

        rev_a, rev_b = await asyncio.gather(_writer(b"v1"), _writer(b"v2"))

        # One got revision 1, the other got revision 2 — NOT a fork.
        assert {rev_a, rev_b} == {1, 2}

        # Verify only 2 rows total (no sibling revisions sharing revision_no).
        async with factory() as s:
            n = await s.scalar(
                text("SELECT COUNT(*) FROM doc.filing WHERE source_filing_ref = 'CONCURRENT'")
            )
            assert n == 2
    finally:
        await engine.dispose()
