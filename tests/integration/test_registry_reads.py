from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.errors import EntityNotFound
from aslan_core.registry.client import EntityRegistryClient

pytestmark = pytest.mark.integration


async def _wipe(session: AsyncSession) -> None:
    """Test isolation: tests commit mid-flow so the function-scoped session's
    rollback can't undo their writes. Clean state up front."""
    for stmt in [
        "DELETE FROM ref.entity_sector",
        "DELETE FROM ref.entity_relationship",
        "DELETE FROM ref.identifier",
        "DELETE FROM ref.entity",
        "DELETE FROM ref.sector",
        "DELETE FROM doc.filing_body",
        "DELETE FROM doc.filing_attachment",
        "DELETE FROM doc.filing",
        "DELETE FROM src.ingestion_run",
        "DELETE FROM src.source",
    ]:
        await session.execute(text(stmt))
    await session.commit()


async def _seed(session: AsyncSession) -> tuple[UUID, int]:
    """Seed: source 'kap' + an ingestion_run + an entity 'Aselsan' with two
    identifiers. Returns (entity_id, ingestion_run_id)."""
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
                "VALUES ('kap', 'seed', 'succeeded') RETURNING ingestion_run_id"
            )
        )
    ).scalar_one()
    eid = (
        await session.execute(
            text(
                "INSERT INTO ref.entity "
                "  (entity_type, legal_name, short_name, status, source_id, ingestion_run_id) "
                "VALUES ('company', 'Aselsan A.Ş.', 'Aselsan', 'active', 'kap', :run) "
                "RETURNING entity_id"
            ),
            {"run": run_id},
        )
    ).scalar_one()
    await session.execute(
        text(
            "INSERT INTO ref.identifier "
            "  (entity_id, namespace, value, is_primary, source_id, ingestion_run_id) "
            "VALUES (:eid, 'kap_entity_code', '19387', true, 'kap', :run), "
            "       (:eid, 'bist_ticker',     'ASELS', true, 'kap', :run)"
        ),
        {"eid": eid, "run": run_id},
    )
    await session.commit()
    return eid, run_id


async def test_resolve_returns_entity_id(session: AsyncSession) -> None:
    await _wipe(session)
    eid, run = await _seed(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    assert await client.resolve("bist_ticker", "ASELS") == eid


async def test_resolve_returns_none_when_absent(session: AsyncSession) -> None:
    await _wipe(session)
    _, run = await _seed(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    assert await client.resolve("bist_ticker", "GHOST") is None


async def test_resolve_or_raise_raises_when_absent(session: AsyncSession) -> None:
    await _wipe(session)
    _, run = await _seed(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    with pytest.raises(EntityNotFound):
        await client.resolve_or_raise("bist_ticker", "GHOST")


async def test_resolve_respects_temporal_window(session: AsyncSession) -> None:
    await _wipe(session)
    _, run = await _seed(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    # Default valid_from = '1900-01-01', valid_to = '9999-12-31'.
    # An as_of in 1990 should still match.
    assert await client.resolve("bist_ticker", "ASELS", as_of=date(1990, 1, 1)) is not None


async def test_get_returns_entity_pydantic(session: AsyncSession) -> None:
    await _wipe(session)
    eid, run = await _seed(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    e = await client.get(eid)
    assert e.entity_id == eid
    assert e.legal_name == "Aselsan A.Ş."
    assert e.type == "company"


async def test_get_raises_when_missing(session: AsyncSession) -> None:
    from uuid import uuid4

    await _wipe(session)
    _, run = await _seed(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    with pytest.raises(EntityNotFound):
        await client.get(uuid4())


async def test_identifiers_for_returns_namespace_map(session: AsyncSession) -> None:
    await _wipe(session)
    eid, run = await _seed(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    ids = await client.identifiers_for(eid)
    assert ids == {"kap_entity_code": "19387", "bist_ticker": "ASELS"}


async def test_search_returns_results_sorted_by_similarity(session: AsyncSession) -> None:
    await _wipe(session)
    eid, run = await _seed(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    results = await client.search("Aselsan", types=["company"])
    assert len(results) >= 1
    assert results[0].entity.entity_id == eid
    assert 0.0 <= results[0].similarity <= 1.0


async def test_search_filters_by_status(session: AsyncSession) -> None:
    """Default status filter is ['active']. A delisted entity shouldn't appear."""
    await _wipe(session)
    _, run = await _seed(session)
    # Add a delisted entity
    await session.execute(
        text(
            "INSERT INTO ref.entity "
            "  (entity_type, legal_name, status, source_id, ingestion_run_id) "
            "VALUES ('company', 'Defunct Aselsan-like Co', 'delisted', 'kap', :run)"
        ),
        {"run": run},
    )
    await session.commit()
    client = EntityRegistryClient(session, ingestion_run_id=run)
    results = await client.search("Aselsan", types=["company"])
    # Only the active 'Aselsan A.Ş.' should appear
    statuses = [r.entity.status for r in results]
    assert all(s == "active" for s in statuses)
