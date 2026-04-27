from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.testing.factories import entity_factory, ingestion_run_factory

pytestmark = pytest.mark.integration


async def _wipe(session: AsyncSession) -> None:
    for stmt in [
        "DELETE FROM ref.entity_sector",
        "DELETE FROM ref.entity_relationship",
        "DELETE FROM ref.identifier",
        "DELETE FROM ref.entity",
        "DELETE FROM src.ingestion_run",
        "DELETE FROM src.source",
    ]:
        await session.execute(text(stmt))
    await session.commit()


async def test_ingestion_run_factory_creates_source_and_run(session: AsyncSession) -> None:
    await _wipe(session)
    run_id = await ingestion_run_factory(session, source_id="test", job_name="job1")
    await session.commit()
    assert run_id > 0
    src = await session.scalar(text("SELECT name FROM src.source WHERE source_id = 'test'"))
    assert src == "test"


async def test_entity_factory_returns_uuid(session: AsyncSession) -> None:
    await _wipe(session)
    run_id = await ingestion_run_factory(session, source_id="test")
    eid = await entity_factory(
        session, ingestion_run_id=run_id, source_id="test", legal_name="Foo Co"
    )
    await session.commit()
    legal = await session.scalar(
        text("SELECT legal_name FROM ref.entity WHERE entity_id = :eid"),
        {"eid": eid},
    )
    assert legal == "Foo Co"


async def test_entity_factory_synthesizes_legal_name(session: AsyncSession) -> None:
    await _wipe(session)
    run_id = await ingestion_run_factory(session, source_id="test")
    eid = await entity_factory(session, ingestion_run_id=run_id, source_id="test")
    await session.commit()
    legal = await session.scalar(
        text("SELECT legal_name FROM ref.entity WHERE entity_id = :eid"),
        {"eid": eid},
    )
    assert legal is not None
    assert legal.startswith("Test Co ")
