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


@pytest.mark.asyncio(loop_scope="session")
async def test_find_by_source_ref_returns_latest_revision(
    session: AsyncSession, object_storage_fake: object
) -> None:
    from aslan_core.documents.client import DocumentStore

    run_id = await _seed(session)

    fid_v1 = uuid4()
    fid_v2 = uuid4()
    await session.execute(
        text(
            "INSERT INTO doc.filing ("
            "  filing_id, source_id, source_filing_ref, kind, title, language, "
            "  published_at, primary_object_key, primary_mime, primary_sha256, "
            "  primary_bytes, ingestion_run_id, revision_no, previous_filing_id, is_amendment"
            ") VALUES "
            "(:f1, 'kap', 'AMEND-1', 'news', 'orig', 'tr', :pub1, 'k1', 'text/html', :s1, 1, :run, 1, NULL, false), "  # noqa: E501
            "(:f2, 'kap', 'AMEND-1', 'news', 'amended', 'tr', :pub2, 'k2', 'text/html', :s2, 1, :run, 2, :f1, true)"  # noqa: E501
        ),
        {
            "f1": fid_v1,
            "pub1": datetime(2026, 4, 27, tzinfo=UTC),
            "s1": "f" * 64,
            "f2": fid_v2,
            "pub2": datetime(2026, 4, 28, tzinfo=UTC),
            "s2": "g" * 64,
            "run": run_id,
        },
    )
    await session.commit()

    store = DocumentStore(session, object_client=object_storage_fake, ingestion_run_id=run_id)  # type: ignore[arg-type]
    found = await store.find_by_source_ref(source_id="kap", source_filing_ref="AMEND-1")
    assert found is not None
    assert found.filing_id == fid_v2
    assert found.revision_no == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_find_by_source_ref_returns_none_when_unknown(
    session: AsyncSession, object_storage_fake: object
) -> None:
    from aslan_core.documents.client import DocumentStore

    run_id = await _seed(session)
    store = DocumentStore(session, object_client=object_storage_fake, ingestion_run_id=run_id)  # type: ignore[arg-type]
    assert await store.find_by_source_ref(source_id="kap", source_filing_ref="NONE") is None


@pytest.mark.asyncio(loop_scope="session")
async def test_amendment_chain_returns_revisions_oldest_first(
    session: AsyncSession, object_storage_fake: object
) -> None:
    from aslan_core.documents.client import DocumentStore

    run_id = await _seed(session)

    fid_v1, fid_v2, fid_v3 = uuid4(), uuid4(), uuid4()
    fid_unrelated = uuid4()
    await session.execute(
        text(
            "INSERT INTO doc.filing ("
            "  filing_id, source_id, source_filing_ref, kind, title, language, "
            "  published_at, primary_object_key, primary_mime, primary_sha256, "
            "  primary_bytes, ingestion_run_id, revision_no, previous_filing_id, is_amendment"
            ") VALUES "
            "(:f1, 'kap', 'CHAIN-1', 'news', 'r1', 'tr', :p1, 'k1', 'text/html', :s1, 1, :run, 1, NULL, false), "  # noqa: E501
            "(:f2, 'kap', 'CHAIN-1', 'news', 'r2', 'tr', :p2, 'k2', 'text/html', :s2, 1, :run, 2, :f1, true), "  # noqa: E501
            "(:f3, 'kap', 'CHAIN-1', 'news', 'r3', 'tr', :p3, 'k3', 'text/html', :s3, 1, :run, 3, :f2, true), "  # noqa: E501
            "(:fu, 'kap', 'UNRELATED', 'news', 'u', 'tr', :p1, 'k4', 'text/html', :su, 1, :run, 1, NULL, false)"  # noqa: E501
        ),
        {
            "f1": fid_v1,
            "p1": datetime(2026, 4, 26, tzinfo=UTC),
            "s1": "h" * 64,
            "f2": fid_v2,
            "p2": datetime(2026, 4, 27, tzinfo=UTC),
            "s2": "i" * 64,
            "f3": fid_v3,
            "p3": datetime(2026, 4, 28, tzinfo=UTC),
            "s3": "j" * 64,
            "fu": fid_unrelated,
            "su": "k" * 64,
            "run": run_id,
        },
    )
    await session.commit()

    store = DocumentStore(session, object_client=object_storage_fake, ingestion_run_id=run_id)  # type: ignore[arg-type]
    chain = await store.amendment_chain(fid_v3)
    assert [f.filing_id for f in chain] == [fid_v1, fid_v2, fid_v3]
