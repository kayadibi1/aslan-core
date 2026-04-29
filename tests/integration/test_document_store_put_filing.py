from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from aslan_core.documents.object_storage import InMemoryFake

pytestmark = pytest.mark.integration


class _FakeWithControlledFailure(InMemoryFake):
    """Test helper: lets a test control which operation raises.

    fail_put_object_substring: any key containing this substring raises.
    fail_delete_object_substring: any delete on a key containing this raises.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fail_put_object_substring: list[str] = []
        self.fail_delete_object_substring: list[str] = []

    async def put_object(self, *, bucket: str, key: str, body: bytes, content_type: str) -> None:
        for s in self.fail_put_object_substring:
            if s in key:
                raise RuntimeError(f"simulated put failure for {key}")
        await super().put_object(bucket=bucket, key=key, body=body, content_type=content_type)

    async def delete_object(self, *, bucket: str, key: str) -> None:
        for s in self.fail_delete_object_substring:
            if s in key:
                raise RuntimeError(f"simulated delete failure for {key}")
        await super().delete_object(bucket=bucket, key=key)


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


@pytest.mark.asyncio(loop_scope="session")
async def test_put_filing_rejects_inconsistent_xbrl_args(
    session: AsyncSession,
    object_storage_fake: object,
) -> None:
    """I3: has_xbrl=True with no bytes/filename, or bytes with has_xbrl=False,
    must raise ValueError before any I/O."""
    from aslan_core.documents.client import DocumentStore

    run_id = await _seed(session)
    store = DocumentStore(
        session,
        object_client=object_storage_fake,  # type: ignore[arg-type]
        ingestion_run_id=run_id,
    )

    base_kwargs: dict[str, object] = {
        "source_id": "kap",
        "source_filing_ref": "XBRL-BAD",
        "entity_id": None,
        "kind": "financial_report",
        "title": "t",
        "published_at": datetime(2026, 4, 28, tzinfo=UTC),
        "primary_bytes": b"x",
        "primary_mime": "text/html",
        "primary_filename": "m.html",
    }
    # has_xbrl=True with no bytes/filename
    with pytest.raises(ValueError, match="requires both"):
        await store.put_filing(**base_kwargs, has_xbrl=True)  # type: ignore[arg-type]
    # bytes provided with has_xbrl=False (default)
    with pytest.raises(ValueError, match="omit the xbrl"):
        await store.put_filing(**base_kwargs, xbrl_bytes=b"x", xbrl_filename="x.xbrl")  # type: ignore[arg-type]


@pytest.mark.asyncio(loop_scope="session")
async def test_put_filing_with_attachments_uploads_each(
    session: AsyncSession,
    object_storage_fake: object,
) -> None:
    from aslan_core.documents.client import DocumentStore
    from aslan_core.schemas.filing import AttachmentIn

    run_id = await _seed(session)
    store = DocumentStore(
        session,
        object_client=object_storage_fake,  # type: ignore[arg-type]
        ingestion_run_id=run_id,
    )

    primary = b"<html>main</html>"
    attachments = [
        AttachmentIn(
            bytes=b"exhibit1",
            mime="application/pdf",
            filename="ex1.pdf",
            role="exhibit",
            sequence=1,
        ),
        AttachmentIn(
            bytes=b"exhibit2",
            mime="application/pdf",
            filename="ex2.pdf",
            role="exhibit",
            sequence=2,
        ),
    ]
    result = await store.put_filing(
        source_id="kap",
        source_filing_ref="WITH-ATTS",
        entity_id=None,
        kind="material_event",
        title="With attachments",
        published_at=datetime(2026, 4, 28, tzinfo=UTC),
        primary_bytes=primary,
        primary_mime="text/html",
        primary_filename="main.html",
        attachments=attachments,
    )
    await session.commit()

    assert result.created is True
    # Manifest reflects primary + 2 attachments
    assert len(result.object_keys) == 3
    # 1 primary + 2 attachments = 3 keys in bucket
    assert len(object_storage_fake.all_keys()) == 3  # type: ignore[attr-defined]
    # 2 rows in doc.filing_attachment
    att_count = await session.scalar(
        text("SELECT COUNT(*) FROM doc.filing_attachment WHERE filing_id = :fid"),
        {"fid": result.filing.filing_id},
    )
    assert att_count == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_put_filing_attachment_re_upload_idempotent(
    session: AsyncSession,
    object_storage_fake: object,
) -> None:
    """ON CONFLICT (filing_id, sha256) DO NOTHING — re-uploading same
    bytes after a partial failure recovers cleanly with no duplicate row."""
    from aslan_core.documents.client import DocumentStore
    from aslan_core.schemas.filing import AttachmentIn

    run_id = await _seed(session)
    store = DocumentStore(
        session,
        object_client=object_storage_fake,  # type: ignore[arg-type]
        ingestion_run_id=run_id,
    )

    primary = b"<html>main</html>"
    a1 = AttachmentIn(
        bytes=b"exhibit1",
        mime="application/pdf",
        filename="ex1.pdf",
        role="exhibit",
        sequence=1,
    )

    # Run 1
    r1 = await store.put_filing(
        source_id="kap",
        source_filing_ref="ATT-IDEMPOTENT",
        entity_id=None,
        kind="news",
        title="t",
        published_at=datetime(2026, 4, 28, tzinfo=UTC),
        primary_bytes=primary,
        primary_mime="text/html",
        primary_filename="main.html",
        attachments=[a1],
    )
    await session.commit()

    # Run 2 — same bytes hash-dedup primary; attachment INSERT idempotent on (filing_id, sha256)
    r2 = await store.put_filing(
        source_id="kap",
        source_filing_ref="ATT-IDEMPOTENT",
        entity_id=None,
        kind="news",
        title="t",
        published_at=datetime(2026, 4, 28, tzinfo=UTC),
        primary_bytes=primary,
        primary_mime="text/html",
        primary_filename="main.html",
        attachments=[a1],
    )
    await session.commit()

    assert r2.created is False  # hash dedup of the primary
    # Same filing_id; attachment count unchanged at 1
    assert r2.filing.filing_id == r1.filing.filing_id
    att_count = await session.scalar(
        text("SELECT COUNT(*) FROM doc.filing_attachment WHERE filing_id = :fid"),
        {"fid": r1.filing.filing_id},
    )
    assert att_count == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_attach_extracted_text_inserts_or_updates(
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

    r = await store.put_filing(
        source_id="kap",
        source_filing_ref="TEXT-1",
        entity_id=None,
        kind="news",
        title="t",
        published_at=datetime(2026, 4, 28, tzinfo=UTC),
        primary_bytes=b"<html>some content</html>",
        primary_mime="text/html",
        primary_filename="main.html",
    )
    await session.commit()

    await store.attach_extracted_text(r.filing.filing_id, "Some extracted text", lang="tr")
    await session.commit()

    body = await session.scalar(
        text("SELECT body_text FROM doc.filing_body WHERE filing_id = :fid"),
        {"fid": r.filing.filing_id},
    )
    assert body == "Some extracted text"

    # Re-extract: latest wins
    await store.attach_extracted_text(r.filing.filing_id, "Updated text", lang="tr")
    await session.commit()
    body2 = await session.scalar(
        text("SELECT body_text FROM doc.filing_body WHERE filing_id = :fid"),
        {"fid": r.filing.filing_id},
    )
    assert body2 == "Updated text"


# ─── Atomicity tests (spec §5.5) ──────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_atomicity_object_upload_fail_raises_objectstoreerror(session: AsyncSession) -> None:
    """Spec §5.5 row 1: object upload fails → no DB write, no leftover blob."""
    from aslan_core.documents.client import DocumentStore
    from aslan_core.errors import ObjectStoreError

    run_id = await _seed(session)

    fake = _FakeWithControlledFailure()
    fake.fail_put_object_substring = ["main.html"]  # primary upload fails

    store = DocumentStore(session, object_client=fake, ingestion_run_id=run_id)
    with pytest.raises(ObjectStoreError):
        await store.put_filing(
            source_id="kap",
            source_filing_ref="UPLOAD-FAIL",
            entity_id=None,
            kind="news",
            title="t",
            published_at=datetime(2026, 4, 28, tzinfo=UTC),
            primary_bytes=b"x",
            primary_mime="text/html",
            primary_filename="main.html",
        )

    # No DB row written
    n = await session.scalar(
        text("SELECT COUNT(*) FROM doc.filing WHERE source_filing_ref = 'UPLOAD-FAIL'")
    )
    assert n == 0
    # No leftover blob
    assert fake.all_keys() == set()


@pytest.mark.asyncio(loop_scope="session")
async def test_atomicity_db_upsert_fail_after_upload_cleans_up_blob(
    session: AsyncSession,
) -> None:
    """Spec §5.5 row 2: object upload OK, DB INSERT fails → best-effort
    blob delete, DocumentDBError raised. Verifies the codex-corrected
    single-outer-try cleanup pattern."""
    from unittest.mock import patch

    from aslan_core.documents.client import DocumentStore
    from aslan_core.errors import DocumentDBError

    run_id = await _seed(session)

    fake = _FakeWithControlledFailure()
    store = DocumentStore(session, object_client=fake, ingestion_run_id=run_id)

    body = b"<html>db-fail-test</html>"

    # Monkey-patch session.execute to raise when the doc.filing INSERT runs.
    real_execute = session.execute

    async def _failing_execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        sql = str(stmt)
        if "INSERT INTO doc.filing " in sql:
            raise RuntimeError("simulated DB failure on doc.filing INSERT")
        return await real_execute(stmt, *args, **kwargs)

    with (
        patch.object(session, "execute", side_effect=_failing_execute),
        pytest.raises(DocumentDBError),
    ):
        await store.put_filing(
            source_id="kap",
            source_filing_ref="DB-FAIL",
            entity_id=None,
            kind="news",
            title="t",
            published_at=datetime(2026, 4, 28, tzinfo=UTC),
            primary_bytes=body,
            primary_mime="text/html",
            primary_filename="main.html",
        )

    # Primary blob was uploaded then deleted by cleanup
    assert (
        fake.all_keys() == set()
    ), "expected primary blob to be deleted by cleanup loop in put_filing's except"


@pytest.mark.asyncio(loop_scope="session")
async def test_atomicity_blob_delete_fail_after_db_fail_logs_orphan(
    session: AsyncSession,
) -> None:
    """Spec §5.5 row 3: blob delete fails after DB failure → original
    error still propagates AND orphan_cleanup_failed log emitted with
    the orphan key for recovery."""
    from unittest.mock import patch

    from aslan_core.documents.client import DocumentStore
    from aslan_core.errors import DocumentDBError

    run_id = await _seed(session)

    fake = _FakeWithControlledFailure()
    fake.fail_delete_object_substring = ["main.html"]  # delete fails for the primary

    store = DocumentStore(session, object_client=fake, ingestion_run_id=run_id)

    body = b"<html>db-fail-and-delete-fail</html>"

    # Monkey-patch DB INSERT to raise
    real_execute = session.execute

    async def _failing_execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        sql = str(stmt)
        if "INSERT INTO doc.filing " in sql:
            raise RuntimeError("simulated DB failure")
        return await real_execute(stmt, *args, **kwargs)

    with (
        capture_logs() as logs,
        patch.object(session, "execute", side_effect=_failing_execute),
        pytest.raises(DocumentDBError),
    ):
        await store.put_filing(
            source_id="kap",
            source_filing_ref="DOUBLE-FAIL",
            entity_id=None,
            kind="news",
            title="t",
            published_at=datetime(2026, 4, 28, tzinfo=UTC),
            primary_bytes=body,
            primary_mime="text/html",
            primary_filename="main.html",
        )

    # The blob is still in the bucket because the cleanup delete failed
    keys = fake.all_keys()
    assert any(
        "main.html" in k for (_, k) in keys
    ), "expected the blob to remain (delete was forced to fail)"

    # Structured orphan_cleanup_failed log emitted
    orphan_logs = [log for log in logs if log.get("event") == "orphan_cleanup_failed"]
    assert len(orphan_logs) >= 1
    log_entry = orphan_logs[0]
    assert log_entry.get("log_level") == "warning"
    # The orphan key should be in the log entry for recovery
    assert "main.html" in log_entry.get("key", "")
    assert log_entry.get("bucket") == "aslan-filings"


@pytest.mark.asyncio(loop_scope="session")
async def test_atomicity_hash_dedup_deletes_just_uploaded_blob(
    session: AsyncSession,
    object_storage_fake: object,
) -> None:
    """Spec §5.5 row 5 + codex 2026-04-28 fix: second put_filing with
    identical bytes hash-dedup hits, just-uploaded blob is deleted, the
    existing row's blob is untouched, returns created=False with the
    existing row's revision_no."""
    from aslan_core.documents.client import DocumentStore

    run_id = await _seed(session)
    store = DocumentStore(session, object_client=object_storage_fake, ingestion_run_id=run_id)  # type: ignore[arg-type]

    body = b"<html>same bytes both calls</html>"

    # First call — fresh insert
    r1 = await store.put_filing(
        source_id="kap",
        source_filing_ref="DEDUP-1",
        entity_id=None,
        kind="news",
        title="t",
        published_at=datetime(2026, 4, 28, tzinfo=UTC),
        primary_bytes=body,
        primary_mime="text/html",
        primary_filename="main.html",
    )
    await session.commit()
    assert r1.created is True
    assert len(object_storage_fake.all_keys()) == 1  # type: ignore[attr-defined]
    initial_keys = object_storage_fake.all_keys()  # type: ignore[attr-defined]

    # Second call — same bytes, same source_ref → hash dedup
    r2 = await store.put_filing(
        source_id="kap",
        source_filing_ref="DEDUP-1",
        entity_id=None,
        kind="news",
        title="t",
        published_at=datetime(2026, 4, 28, tzinfo=UTC),
        primary_bytes=body,
        primary_mime="text/html",
        primary_filename="main.html",
    )
    await session.commit()

    # created=False; revision_no matches first call (same row)
    assert r2.created is False
    assert r2.revision_no == r1.revision_no
    assert r2.filing.filing_id == r1.filing.filing_id

    # Bucket still has exactly the original blob (the just-uploaded
    # blob from the second call was deleted by the codex fix)
    assert object_storage_fake.all_keys() == initial_keys  # type: ignore[attr-defined]

    # Manifest is empty (no committed objects from the dedup-hit call)
    assert r2.object_keys == []


@pytest.mark.asyncio(loop_scope="session")
async def test_atomicity_attachment_insert_fail_outer_cleanup_deletes_everything(
    session: AsyncSession,
) -> None:
    """Spec §5.5 + codex 2026-04-28: per-attachment INSERT failure trips
    put_filing's outer cleanup which deletes EVERY uploaded blob (primary
    + every attachment). No per-attachment retention — all the rows are
    in the same uncommitted transaction and roll back together."""
    from unittest.mock import patch

    from aslan_core.documents.client import DocumentStore
    from aslan_core.errors import DocumentDBError
    from aslan_core.schemas.filing import AttachmentIn

    run_id = await _seed(session)

    fake = _FakeWithControlledFailure()
    store = DocumentStore(session, object_client=fake, ingestion_run_id=run_id)

    # Monkey-patch so the SECOND attachment's INSERT raises (first one
    # succeeds-then-rolls-back, second one's INSERT raises).
    real_execute = session.execute
    insert_attachment_count = 0

    async def _failing_execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal insert_attachment_count
        sql = str(stmt)
        if "INSERT INTO doc.filing_attachment" in sql:
            insert_attachment_count += 1
            if insert_attachment_count == 2:
                raise RuntimeError("simulated DB failure on 2nd attachment INSERT")
        return await real_execute(stmt, *args, **kwargs)

    primary = b"<html>main</html>"
    attachments = [
        AttachmentIn(
            bytes=b"first", mime="application/pdf", filename="a1.pdf", role="exhibit", sequence=1
        ),
        AttachmentIn(
            bytes=b"second", mime="application/pdf", filename="a2.pdf", role="exhibit", sequence=2
        ),
        AttachmentIn(
            bytes=b"third", mime="application/pdf", filename="a3.pdf", role="exhibit", sequence=3
        ),
    ]

    with (
        patch.object(session, "execute", side_effect=_failing_execute),
        pytest.raises(DocumentDBError),
    ):
        await store.put_filing(
            source_id="kap",
            source_filing_ref="ATT-FAIL",
            entity_id=None,
            kind="material_event",
            title="t",
            published_at=datetime(2026, 4, 28, tzinfo=UTC),
            primary_bytes=primary,
            primary_mime="text/html",
            primary_filename="main.html",
            attachments=attachments,
        )

    # After the failure, the outer cleanup deletes EVERY uploaded blob:
    # primary + first attachment + second attachment (uploaded before its
    # INSERT failed). Third attachment was never uploaded.
    assert fake.all_keys() == set(), f"expected all blobs cleaned up; got {fake.all_keys()}"

    # No doc.filing rows — the failed INSERT and the prior failing INSERTs
    # all share the same transaction; SQLAlchemy's session is now in an
    # error state. Roll back before querying.
    await session.rollback()
    n_filings = await session.scalar(
        text("SELECT COUNT(*) FROM doc.filing WHERE source_filing_ref = 'ATT-FAIL'")
    )
    assert n_filings == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_release_result_deletes_all_manifest_keys(
    session: AsyncSession,
    object_storage_fake: object,
) -> None:
    """Caller put_filing → release(result) deletes every blob in the
    manifest (primary + xbrl + attachments). codex 2026-04-28: release()
    reads from PutFilingResult.object_keys, NOT from doc.filing_attachment."""
    from aslan_core.documents.client import DocumentStore
    from aslan_core.schemas.filing import AttachmentIn

    run_id = await _seed(session)
    store = DocumentStore(session, object_client=object_storage_fake, ingestion_run_id=run_id)  # type: ignore[arg-type]

    primary = b"<html>main</html>"
    xbrl = b"<xbrl>data</xbrl>"
    attachments = [
        AttachmentIn(
            bytes=b"ex1", mime="application/pdf", filename="ex1.pdf", role="exhibit", sequence=1
        ),
        AttachmentIn(
            bytes=b"ex2", mime="application/pdf", filename="ex2.pdf", role="exhibit", sequence=2
        ),
    ]

    result = await store.put_filing(
        source_id="kap",
        source_filing_ref="RELEASE-1",
        entity_id=None,
        kind="financial_report",
        title="With everything",
        published_at=datetime(2026, 4, 28, tzinfo=UTC),
        primary_bytes=primary,
        primary_mime="text/html",
        primary_filename="main.html",
        attachments=attachments,
        has_xbrl=True,
        xbrl_bytes=xbrl,
        xbrl_filename="report.xbrl",
    )
    await session.commit()

    # Bucket has 4 blobs (primary + xbrl + 2 attachments)
    assert len(object_storage_fake.all_keys()) == 4  # type: ignore[attr-defined]
    assert len(result.object_keys) == 4

    # Caller decides to roll back. release(result) cleans up the bucket.
    await store.release(result)

    # All blobs gone
    assert object_storage_fake.all_keys() == set()  # type: ignore[attr-defined]
