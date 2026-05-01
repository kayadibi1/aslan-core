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


# ─── audit-content tests for Task 9 ────────────────────────────────────


async def test_ingestion_run_with_actor_emits_start_and_complete_events(
    engine: AsyncEngine,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ingestion_run(actor=) sets the actor on the ContextVar for the
    scope and emits ingestion_run.start (at entry) +
    ingestion_run.complete (at exit). The src.ingestion_run row's
    denormalised audit cols also reflect the actor."""
    from aslan_core.audit import Actor

    await _seed_source(session, "kap")
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.events"))
        await s.commit()

    actor = Actor(actor_id="user:cron", actor_kind="user")
    async with ingestion_run(engine, source_id="kap", job_name="audited", actor=actor) as run:
        run_id = run.id

    async with session_factory() as s:
        # Row's audit cols reflect actor.
        row = (
            await s.execute(
                text(
                    "SELECT actor_id, actor_kind FROM src.ingestion_run "
                    "WHERE ingestion_run_id = :id"
                ),
                {"id": run_id},
            )
        ).one()
        assert row.actor_id == "user:cron"
        assert row.actor_kind == "user"

        events = (
            await s.execute(
                text(
                    "SELECT operation, actor_id FROM audit.events "
                    "WHERE target_schema = 'src' AND target_table = 'ingestion_run' "
                    "  AND ingestion_run_id = :id "
                    "ORDER BY occurred_at"
                ),
                {"id": run_id},
            )
        ).all()
        ops = [e.operation for e in events]
        assert "ingestion_run.start" in ops
        assert "ingestion_run.complete" in ops
        for e in events:
            assert e.actor_id == "user:cron"


async def test_ingestion_run_set_metadata_emits_audit_event(
    engine: AsyncEngine,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Handle.set_metadata buffers an audit event that flushes to
    audit.events alongside the close-run UPDATE."""
    from aslan_core.audit import Actor

    await _seed_source(session, "kap")
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.events"))
        await s.commit()

    actor = Actor(actor_id="user:meta", actor_kind="user")
    async with ingestion_run(engine, source_id="kap", job_name="meta", actor=actor) as run:
        run.set_metadata({"k": "v"})
        run_id = run.id

    async with session_factory() as s:
        events = (
            await s.execute(
                text(
                    "SELECT operation, after FROM audit.events "
                    "WHERE target_schema = 'src' AND ingestion_run_id = :id "
                    "  AND operation = 'ingestion_run.set_metadata'"
                ),
                {"id": run_id},
            )
        ).all()
        assert len(events) == 1
        assert events[0].after["metadata"] == {"k": "v"}


async def test_ingestion_run_increment_rows_emits_audit_event(
    engine: AsyncEngine,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Handle.increment_rows emits one audit event per call with the
    rows-counter delta. Audit row goes through the run's connection."""
    from aslan_core.audit import Actor

    await _seed_source(session, "kap")
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.events"))
        await s.commit()

    actor = Actor(actor_id="user:rows", actor_kind="user")
    async with ingestion_run(engine, source_id="kap", job_name="rows", actor=actor) as run:
        await run.increment_rows(3)
        await run.increment_rows(5)
        run_id = run.id

    async with session_factory() as s:
        events = (
            await s.execute(
                text(
                    "SELECT operation, before, after FROM audit.events "
                    "WHERE target_schema = 'src' AND ingestion_run_id = :id "
                    "  AND operation = 'ingestion_run.increment_rows' "
                    "ORDER BY occurred_at, event_id"
                ),
                {"id": run_id},
            )
        ).all()
        assert len(events) == 2
        assert events[0].after["rows"] == 3
        assert events[1].after["rows"] == 8
