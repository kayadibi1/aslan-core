"""Codex F3, 2026-04-29: strict-mode regression tests.

Proves that ``Settings.audit_strict=True`` actually rejects REAL
mutations when no actor is set in the ContextVar. The procurement-
grade default for any customer-facing service profile (aslan-service
from p0.1 onward) depends on this raising BEFORE any I/O — no DB
INSERT, no blob upload — so customers cannot accidentally write rows
without attribution.

This file opts OUT of the conftest's autouse default-actor fixture by
declaring a local autouse fixture that runs AFTER conftest's (Pytest
applies file-local autouse fixtures after conftest's). The local
fixture re-clears ``_current_actor`` so strict mode actually sees no
actor — without that, every mutation here would inherit
``user:pytest`` from the conftest default and the strict raise would
never fire.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import set_actor
from aslan_core.config import Settings
from aslan_core.errors import AuditMissingActor

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_default_actor() -> Iterator[None]:
    """Opt out of conftest's autouse default-actor fixture by clearing
    the ContextVar. Runs AFTER conftest's fixture (Pytest applies
    file-local autouse fixtures last), so the previous fixture's
    ``set_actor(Actor(...))`` is undone before the test body runs."""
    set_actor(None)
    yield
    set_actor(None)


async def _seed_source_and_run(session: AsyncSession) -> int:
    """Seed minimal src.source + src.ingestion_run rows. The autouse
    default-actor fixture is bypassed in this file, so seeds use raw
    SQL (no audit emission) to keep the strict-mode test focused on
    the public mutation methods."""
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('kap', 'KAP', 'scraper', 'open') ON CONFLICT DO NOTHING"
        )
    )
    run_id: int = (
        await session.execute(
            text(
                "INSERT INTO src.ingestion_run (source_id, job_name, status) "
                "VALUES ('kap', 'strict_test', 'succeeded') "
                "RETURNING ingestion_run_id"
            )
        )
    ).scalar_one()
    await session.commit()
    return run_id


@pytest.mark.asyncio(loop_scope="session")
async def test_strict_mode_create_entity_raises_without_actor(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_entity with no actor and audit_strict=True must raise
    AuditMissingActor BEFORE any DB INSERT. Asserts:
      - the exception is raised
      - no ref.entity row was inserted
      - no audit.events row was written
    """
    monkeypatch.setenv("ASLAN_AUDIT_STRICT", "true")
    # Sanity: the env var actually flows into Settings.
    assert Settings(_env_file=None).audit_strict is True

    from aslan_core.registry.client import EntityRegistryClient

    run_id = await _seed_source_and_run(session)
    # Snapshot pre-state so we can assert "no rows added" precisely.
    pre_entities = await session.scalar(
        text("SELECT COUNT(*) FROM ref.entity WHERE legal_name = 'StrictX'")
    )
    pre_events = await session.scalar(text("SELECT COUNT(*) FROM audit.events"))

    client = EntityRegistryClient(session, ingestion_run_id=run_id)
    with pytest.raises(AuditMissingActor):
        await client.create_entity(
            type="company",
            legal_name="StrictX",
            identifiers={"kap_entity_code": "STRICT-1"},
        )

    # No row was inserted: the early check fired BEFORE the INSERT.
    post_entities = await session.scalar(
        text("SELECT COUNT(*) FROM ref.entity WHERE legal_name = 'StrictX'")
    )
    post_events = await session.scalar(text("SELECT COUNT(*) FROM audit.events"))
    assert post_entities == pre_entities
    assert post_events == pre_events


@pytest.mark.asyncio(loop_scope="session")
async def test_strict_mode_upsert_sector_raises_without_actor(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex Batch 2 F1: upsert_sector with no actor and audit_strict=True
    must raise AuditMissingActor BEFORE any DB INSERT/UPDATE on
    ref.sector. Asserts:
      - the exception is raised
      - no ref.sector row was created/changed
      - no audit.events row was written
    """
    monkeypatch.setenv("ASLAN_AUDIT_STRICT", "true")
    assert Settings(_env_file=None).audit_strict is True

    from aslan_core.registry.client import EntityRegistryClient

    run_id = await _seed_source_and_run(session)
    sector_id = "bist:STRICT-XBANK"
    pre_sectors = await session.scalar(
        text("SELECT COUNT(*) FROM ref.sector WHERE sector_id = :sid"),
        {"sid": sector_id},
    )
    pre_events = await session.scalar(text("SELECT COUNT(*) FROM audit.events"))

    client = EntityRegistryClient(session, ingestion_run_id=run_id)
    with pytest.raises(AuditMissingActor):
        await client.upsert_sector(
            sector_id=sector_id,
            taxonomy="bist",
            code="STRICT-XBANK",
            name_tr="Bankalar",
        )

    post_sectors = await session.scalar(
        text("SELECT COUNT(*) FROM ref.sector WHERE sector_id = :sid"),
        {"sid": sector_id},
    )
    post_events = await session.scalar(text("SELECT COUNT(*) FROM audit.events"))
    assert post_sectors == pre_sectors
    assert post_events == pre_events


@pytest.mark.asyncio(loop_scope="session")
async def test_strict_mode_put_filing_raises_without_actor(
    session: AsyncSession,
    object_storage_fake: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """put_filing with no actor and audit_strict=True must raise
    AuditMissingActor BEFORE any blob upload or DB INSERT.

    This is the procurement-grade contract: strict mode is meaningful
    only if it catches missing actors before any side effect runs.
    Asserts:
      - the exception is raised
      - the bucket is empty (no primary blob uploaded)
      - the caller-side filing row count is unchanged
    """
    monkeypatch.setenv("ASLAN_AUDIT_STRICT", "true")
    assert Settings(_env_file=None).audit_strict is True

    from aslan_core.documents.client import DocumentStore

    run_id = await _seed_source_and_run(session)
    pre_filings = await session.scalar(
        text("SELECT COUNT(*) FROM doc.filing WHERE source_filing_ref = 'STRICT-FILING-1'")
    )

    store = DocumentStore(
        session,
        object_client=object_storage_fake,  # type: ignore[arg-type]
        ingestion_run_id=run_id,
    )
    with pytest.raises(AuditMissingActor):
        await store.put_filing(
            source_id="kap",
            source_filing_ref="STRICT-FILING-1",
            entity_id=None,
            kind="news",
            title="Strict",
            published_at=datetime(2026, 4, 28, tzinfo=UTC),
            primary_bytes=b"<html>strict</html>",
            primary_mime="text/html",
            primary_filename="main.html",
        )

    # The bucket is empty: strict mode caught the missing actor BEFORE
    # any object upload ran (procurement-grade contract).
    assert object_storage_fake.all_keys() == set()  # type: ignore[attr-defined]
    # No filing row was inserted.
    post_filings = await session.scalar(
        text("SELECT COUNT(*) FROM doc.filing WHERE source_filing_ref = 'STRICT-FILING-1'")
    )
    assert post_filings == pre_filings
