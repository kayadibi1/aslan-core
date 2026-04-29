from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.errors import DocumentNotFound

pytestmark = pytest.mark.integration


async def _seed(session: AsyncSession) -> int:
    """Wipe + insert 'kap' source + ingestion_run; return run_id."""
    await session.execute(text("DELETE FROM doc.filing_body"))
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
                "VALUES ('kap', 'doc_store_test', 'succeeded') RETURNING ingestion_run_id"
            )
        )
    ).scalar_one()
    await session.commit()
    return cast(int, run_id)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_filing_returns_pydantic_model(
    session: AsyncSession, object_storage_fake: object
) -> None:
    from aslan_core.documents.client import DocumentStore

    run_id = await _seed(session)

    fid = uuid4()
    await session.execute(
        text(
            "INSERT INTO doc.filing ("
            "  filing_id, source_id, source_filing_ref, kind, title, language, "
            "  published_at, primary_object_key, primary_mime, primary_sha256, "
            "  primary_bytes, ingestion_run_id, revision_no"
            ") VALUES ("
            "  :id, 'kap', 'GET-1', 'news', 't', 'tr', "
            "  :pub, 'k', 'text/html', :sha, 1, :run, 1)"
        ),
        {"id": fid, "pub": datetime(2026, 4, 28, tzinfo=UTC), "sha": "e" * 64, "run": run_id},
    )
    await session.commit()

    store = DocumentStore(session, object_client=object_storage_fake, ingestion_run_id=run_id)  # type: ignore[arg-type]
    f = await store.get_filing(fid)
    assert f.filing_id == fid
    assert f.source_filing_ref == "GET-1"


@pytest.mark.asyncio(loop_scope="session")
async def test_get_filing_raises_on_missing(
    session: AsyncSession, object_storage_fake: object
) -> None:
    from aslan_core.documents.client import DocumentStore

    run_id = await _seed(session)
    store = DocumentStore(session, object_client=object_storage_fake, ingestion_run_id=run_id)  # type: ignore[arg-type]
    with pytest.raises(DocumentNotFound):
        await store.get_filing(uuid4())
