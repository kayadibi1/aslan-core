from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.registry.client import EntityRegistryClient

pytestmark = pytest.mark.integration


async def _wipe(session: AsyncSession) -> None:
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


async def test_link_idempotent_and_updates_weight(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    parent = await client.create_entity(
        type="fund",
        legal_name="P",
        identifiers={"tefas_founder_code": "ABC"},
    )
    child = await client.create_entity(
        type="fund",
        legal_name="C",
        identifiers={"tefas_code": "ABCDEF"},
    )
    await client.link(parent.entity_id, child.entity_id, "fund_of_founder")
    await client.link(parent.entity_id, child.entity_id, "fund_of_founder", weight=Decimal("0.5"))
    await session.commit()

    n = await session.scalar(
        text("SELECT count(*) FROM ref.entity_relationship WHERE parent_id = :p AND child_id = :c"),
        {"p": parent.entity_id, "c": child.entity_id},
    )
    assert n == 1
    w = await session.scalar(
        text("SELECT weight FROM ref.entity_relationship WHERE parent_id = :p AND child_id = :c"),
        {"p": parent.entity_id, "c": child.entity_id},
    )
    assert w == Decimal("0.5")


async def test_upsert_and_assign_sector(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    e = await client.create_entity(
        type="company",
        legal_name="Bank",
        identifiers={"kap_entity_code": "99"},
    )
    await client.upsert_sector(
        sector_id="bist:XBANK",
        taxonomy="bist",
        code="XBANK",
        name_tr="Bankalar",
    )
    await client.assign_sector(e.entity_id, "bist:XBANK", is_primary=True)
    await client.assign_sector(e.entity_id, "bist:XBANK", is_primary=True)  # idempotent
    await session.commit()
    n = await session.scalar(
        text("SELECT count(*) FROM ref.entity_sector WHERE entity_id = :eid"),
        {"eid": e.entity_id},
    )
    assert n == 1


async def test_upsert_sector_updates_existing(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    await client.upsert_sector(
        sector_id="bist:XBANK",
        taxonomy="bist",
        code="XBANK",
        name_tr="Old",
    )
    await client.upsert_sector(
        sector_id="bist:XBANK",
        taxonomy="bist",
        code="XBANK",
        name_tr="Bankalar",
        name_en="Banks",
    )
    await session.commit()
    row = (
        await session.execute(
            text("SELECT name_tr, name_en FROM ref.sector WHERE sector_id = 'bist:XBANK'")
        )
    ).one()
    assert row.name_tr == "Bankalar"
    assert row.name_en == "Banks"
