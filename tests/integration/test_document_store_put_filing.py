from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _seed(session: AsyncSession) -> int:
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
    run_id_row = await session.execute(
        text(
            "INSERT INTO src.ingestion_run (source_id, job_name, status) "
            "VALUES ('kap', 'put_test', 'succeeded') RETURNING ingestion_run_id"
        )
    )
    run_id: int = run_id_row.scalar_one()
    await session.commit()
    return run_id


@pytest.mark.asyncio(loop_scope="session")
async def test_put_filing_fresh_insert_uploads_to_bucket_and_writes_row(
    session: AsyncSession,
    object_storage_fake: object,
) -> None:
    from aslan_core.documents.client import DocumentStore

    run_id = await _seed(session)
    store = DocumentStore(
        session,
        object_client=object_storage_fake,  # type: ignore[arg-type]
        ingestion_run_id=run_id,
    )

    body = b"<html>filing body</html>"
    expected_sha = hashlib.sha256(body).hexdigest()

    result = await store.put_filing(
        source_id="kap",
        source_filing_ref="FRESH-1",
        entity_id=None,
        kind="news",
        title="Fresh insert",
        published_at=datetime(2026, 4, 28, 12, tzinfo=UTC),
        primary_bytes=body,
        primary_mime="text/html",
        primary_filename="main.html",
    )
    await session.commit()

    assert result.created is True
    assert result.is_revision is False
    assert result.revision_no == 1
    assert result.filing.primary_sha256 == expected_sha
    assert result.filing.source_filing_ref == "FRESH-1"
    # Codex-fix manifest: bucket + object_keys
    assert result.bucket == "aslan-filings"
    assert len(result.object_keys) == 1
    primary_key = result.object_keys[0]
    # Primary key should match doc.filing.primary_object_key
    assert primary_key == result.filing.primary_object_key

    # Bucket has exactly the primary blob, body matches
    keys = object_storage_fake.all_keys()  # type: ignore[attr-defined]
    assert len(keys) == 1
    bucket, key = next(iter(keys))
    assert bucket == "aslan-filings"
    assert object_storage_fake.get_body(bucket, key) == body  # type: ignore[attr-defined]


@pytest.mark.asyncio(loop_scope="session")
async def test_put_filing_with_xbrl_uploads_xbrl_blob_and_sets_xbrl_object_key(
    session: AsyncSession,
    object_storage_fake: object,
) -> None:
    """XBRL inputs MUST be uploaded and bound to xbrl_object_key (the
    codex 2026-04-28 review caught silent data loss here)."""
    from aslan_core.documents.client import DocumentStore

    run_id = await _seed(session)
    store = DocumentStore(
        session,
        object_client=object_storage_fake,  # type: ignore[arg-type]
        ingestion_run_id=run_id,
    )

    body = b"<html>filing</html>"
    xbrl = b"<xbrl>data</xbrl>"

    result = await store.put_filing(
        source_id="kap",
        source_filing_ref="XBRL-1",
        entity_id=None,
        kind="financial_report",
        title="With XBRL",
        published_at=datetime(2026, 4, 28, tzinfo=UTC),
        primary_bytes=body,
        primary_mime="text/html",
        primary_filename="main.html",
        has_xbrl=True,
        xbrl_bytes=xbrl,
        xbrl_filename="report.xbrl",
    )
    await session.commit()

    # Both blobs uploaded
    assert len(result.object_keys) == 2
    assert len(object_storage_fake.all_keys()) == 2  # type: ignore[attr-defined]
    # xbrl_object_key persisted on the row
    xbrl_key_row = await session.scalar(
        text("SELECT xbrl_object_key FROM doc.filing WHERE filing_id = :id"),
        {"id": result.filing.filing_id},
    )
    assert xbrl_key_row is not None
    assert "report.xbrl" in xbrl_key_row
    # The bytes match
    bucket = result.bucket
    assert object_storage_fake.get_body(bucket, xbrl_key_row) == xbrl  # type: ignore[attr-defined]
