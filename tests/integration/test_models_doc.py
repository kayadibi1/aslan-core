from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_filing_orm_round_trip(session: AsyncSession) -> None:
    from aslan_core.models.doc import Filing as FilingORM

    # Wipe + ensure 'kap' source + ingestion_run row
    await session.execute(text("DELETE FROM doc.filing_attachment"))
    await session.execute(text("DELETE FROM doc.filing"))
    await session.execute(text("DELETE FROM src.ingestion_run"))
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('kap', 'KAP', 'scraper', 'open') ON CONFLICT DO NOTHING"
        )
    )
    run_id = (
        await session.execute(
            text(
                "INSERT INTO src.ingestion_run (source_id, job_name, status) "
                "VALUES ('kap', 'unit_test', 'succeeded') RETURNING ingestion_run_id"
            )
        )
    ).scalar_one()
    await session.commit()

    f = FilingORM(
        filing_id=uuid4(),
        source_id="kap",
        source_filing_ref="ROUND-TRIP-1",
        entity_id=None,
        kind="news",
        subkind=None,
        title="Round trip",
        language="tr",
        published_at=datetime(2026, 4, 28, 12, tzinfo=UTC),
        period_start=None,
        period_end=None,
        source_url=None,
        is_amendment=False,
        previous_filing_id=None,
        primary_object_key="kap/x/2026/04/28/y/main.html",
        primary_mime="text/html",
        primary_sha256="d" * 64,
        primary_bytes=100,
        extracted_text_key=None,
        has_xbrl=False,
        xbrl_object_key=None,
        metadata_={},
        ingestion_run_id=run_id,
        revision_no=1,
    )
    session.add(f)
    await session.commit()

    fetched = (
        await session.execute(
            select(FilingORM).where(FilingORM.source_filing_ref == "ROUND-TRIP-1")
        )
    ).scalar_one()
    assert fetched.revision_no == 1
    assert fetched.primary_sha256 == "d" * 64
