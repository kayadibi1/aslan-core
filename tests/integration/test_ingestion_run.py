from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aslan_core.errors import UnknownSource
from aslan_core.ingestion.run import ingestion_run

pytestmark = pytest.mark.integration


async def _seed_source(session: AsyncSession, source_id: str = "kap") -> None:
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES (:sid, 'KAP', 'scraper', 'open') "
            "ON CONFLICT (source_id) DO NOTHING"
        ),
        {"sid": source_id},
    )
    await session.commit()


async def test_unknown_source_raises(engine: AsyncEngine) -> None:
    with pytest.raises(UnknownSource) as ei:
        async with ingestion_run(engine, source_id="ghost", job_name="x"):
            pass
    assert ei.value.source_id == "ghost"


async def test_run_succeeds_records_status_and_counters(
    engine: AsyncEngine,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_source(session, "kap")
    async with ingestion_run(engine, source_id="kap", job_name="entity_catalog") as run:
        assert run.source_id == "kap"
        await run.increment_rows(3)
        await run.increment_docs(2)
        await run.increment_bytes(1000)
        run_id = run.id

    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, rows_written, docs_written, bytes_written, error_count "
                    "FROM src.ingestion_run WHERE ingestion_run_id = :id"
                ),
                {"id": run_id},
            )
        ).one()
    assert row.status == "succeeded"
    assert row.rows_written == 3
    assert row.docs_written == 2
    assert row.bytes_written == 1000
    assert row.error_count == 0


async def test_run_failure_records_status_and_truncated_traceback(
    engine: AsyncEngine,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_source(session, "kap")

    class Boom(Exception):
        pass

    captured_id: int | None = None
    with pytest.raises(Boom):
        async with ingestion_run(engine, source_id="kap", job_name="boom") as run:
            captured_id = run.id
            await run.increment_errors(2)
            raise Boom("kaboom")

    assert captured_id is not None
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, error, error_count "
                    "FROM src.ingestion_run WHERE ingestion_run_id = :id"
                ),
                {"id": captured_id},
            )
        ).one()
    assert row.status == "failed"
    assert row.error is not None
    assert "Boom" in row.error
    assert row.error_count == 2


async def test_run_row_visible_during_run(
    engine: AsyncEngine,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The run row commits at entry so other connections can see status='running'."""
    await _seed_source(session, "kap")
    async with (
        ingestion_run(engine, source_id="kap", job_name="visible") as run,
        session_factory() as s,
    ):
        row = (
            await s.execute(
                text("SELECT status FROM src.ingestion_run WHERE ingestion_run_id = :id"),
                {"id": run.id},
            )
        ).one()
        assert row.status == "running"
