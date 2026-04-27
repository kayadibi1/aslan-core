from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.errors import EntityNotFound, IdentifierConflict
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


async def _new_run(session: AsyncSession, source_id: str = "kap") -> int:
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
                "VALUES (:sid, 'job', 'succeeded') RETURNING ingestion_run_id"
            ),
            {"sid": source_id},
        )
    ).scalar_one()
    await session.commit()
    return run_id


async def test_add_identifier_idempotent(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    e = await client.create_entity(
        type="company",
        legal_name="X",
        identifiers={"kap_entity_code": "1"},
    )
    await session.commit()
    await client.add_identifier(e.entity_id, "bist_ticker", "X")
    await client.add_identifier(e.entity_id, "bist_ticker", "X")  # no-op
    await session.commit()


async def test_add_identifier_collision_raises(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    a = await client.create_entity(
        type="company", legal_name="A", identifiers={"kap_entity_code": "1"}
    )
    b = await client.create_entity(
        type="company", legal_name="B", identifiers={"kap_entity_code": "2"}
    )
    await session.commit()
    assert a.entity_id != b.entity_id
    with pytest.raises(IdentifierConflict):
        await client.add_identifier(b.entity_id, "kap_entity_code", "1")


async def test_expire_identifier_makes_resolution_return_none(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    e = await client.create_entity(
        type="company",
        legal_name="X",
        identifiers={"bist_ticker": "OLD"},
    )
    await session.commit()
    await client.expire_identifier("bist_ticker", "OLD", as_of=date(2026, 4, 27))
    await session.commit()
    # On expiry day itself: gone (FIRST INVALID DAY)
    assert await client.resolve("bist_ticker", "OLD", as_of=date(2026, 4, 27)) is None
    # The day before: still resolves
    assert await client.resolve("bist_ticker", "OLD", as_of=date(2026, 4, 26)) == e.entity_id


async def test_expire_identifier_raises_when_no_active(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    with pytest.raises(EntityNotFound):
        await client.expire_identifier("bist_ticker", "GHOST", as_of=date(2026, 4, 27))


async def test_update_entity_partial_patch(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    e = await client.create_entity(
        type="company", legal_name="X", identifiers={"kap_entity_code": "9"}
    )
    await session.commit()
    e2 = await client.update_entity(e.entity_id, short_name="Xco", status="suspended")
    await session.commit()
    assert e2.short_name == "Xco"
    assert e2.status == "suspended"
    assert e2.legal_name == "X"  # unchanged
