from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.errors import EntityMergeRequired
from aslan_core.registry.client import EntityRegistryClient

pytestmark = pytest.mark.integration


async def _wipe(session: AsyncSession) -> None:
    for stmt in [
        "DELETE FROM ref.entity_sector",
        "DELETE FROM ref.entity_relationship",
        "DELETE FROM ref.identifier",
        "DELETE FROM ref.entity",
        "DELETE FROM ref.sector",
        "DELETE FROM src.ingestion_run",
        "DELETE FROM src.source",
    ]:
        await session.execute(text(stmt))
    await session.commit()


async def _new_run(session: AsyncSession, source_id: str = "kap", job: str = "seed") -> int:
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES (:sid, :sid, 'scraper', 'open') ON CONFLICT DO NOTHING"
        ),
        {"sid": source_id},
    )
    run_id: int = (
        await session.execute(
            text(
                "INSERT INTO src.ingestion_run (source_id, job_name, status) "
                "VALUES (:sid, :job, 'succeeded') RETURNING ingestion_run_id"
            ),
            {"sid": source_id, "job": job},
        )
    ).scalar_one()
    await session.commit()
    return run_id


async def test_create_entity_inserts_with_identifiers(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    e = await client.create_entity(
        type="company",
        legal_name="Aselsan A.Ş.",
        identifiers={"kap_entity_code": "19387", "bist_ticker": "ASELS"},
    )
    await session.commit()
    assert e.legal_name == "Aselsan A.Ş."
    assert await client.resolve("bist_ticker", "ASELS") == e.entity_id


async def test_create_entity_idempotent_same_source(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    e1 = await client.create_entity(
        type="company",
        legal_name="Aselsan A.Ş.",
        identifiers={"kap_entity_code": "19387"},
    )
    await session.commit()

    run2 = await _new_run(session, source_id="kap", job="seed_again")
    client2 = EntityRegistryClient(session, ingestion_run_id=run2)
    e2 = await client2.create_entity(
        type="company",
        legal_name="Aselsan A.Ş.",
        identifiers={"kap_entity_code": "19387", "bist_ticker": "ASELS"},
    )
    await session.commit()
    assert e1.entity_id == e2.entity_id
    assert await client2.resolve("bist_ticker", "ASELS") == e2.entity_id


async def test_create_entity_cross_source_match_raises(session: AsyncSession) -> None:
    await _wipe(session)
    run_kap = await _new_run(session, source_id="kap")
    client_kap = EntityRegistryClient(session, ingestion_run_id=run_kap)
    await client_kap.create_entity(
        type="company",
        legal_name="Aselsan A.Ş.",
        identifiers={"bist_ticker": "ASELS"},
    )
    await session.commit()

    run_manual = await _new_run(session, source_id="manual")
    client_manual = EntityRegistryClient(session, ingestion_run_id=run_manual)
    with pytest.raises(EntityMergeRequired):
        await client_manual.create_entity(
            type="company",
            legal_name="Aselsan A.Ş.",
            identifiers={"bist_ticker": "ASELS"},
        )


async def test_create_entity_multi_entity_match_raises(session: AsyncSession) -> None:
    """Two identifiers each currently resolving to a *different* entity."""
    await _wipe(session)
    run = await _new_run(session, source_id="kap")
    client = EntityRegistryClient(session, ingestion_run_id=run)
    await client.create_entity(
        type="company",
        legal_name="Foo A.Ş.",
        identifiers={"kap_entity_code": "1", "bist_ticker": "FOO"},
    )
    await client.create_entity(
        type="company",
        legal_name="Bar A.Ş.",
        identifiers={"kap_entity_code": "2", "bist_ticker": "BAR"},
    )
    await session.commit()

    with pytest.raises(EntityMergeRequired):
        await client.create_entity(
            type="company",
            legal_name="Foo or Bar?",
            identifiers={"bist_ticker": "FOO", "kap_entity_code": "2"},
        )
