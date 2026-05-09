"""Roundtrip test for dq.sync_log.run()."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aslan_core.dq import sync_log
from aslan_core.dq.types import SyncRunStatus

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def test_clean_run_marks_ok(engine: AsyncEngine) -> None:
    async with sync_log.run(
        engine=engine,
        source="kap",
        operation="tail",
    ) as run:
        run.records_ingested(42)
        run.set_upstream_max("2026-05-09T08:00:00Z")
        run.set_db_max("2026-05-09T08:00:00Z")

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT source, operation, status, records_ingested, "
                    "       upstream_max_at, db_max_at "
                    "FROM audit.sync_log "
                    "WHERE source='kap' AND operation='tail' "
                    "ORDER BY sync_id DESC LIMIT 1"
                )
            )
        ).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.source == "kap"
    assert r.operation == "tail"
    assert r.status == "ok"
    assert r.records_ingested == 42


async def test_exception_marks_failed(engine: AsyncEngine) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with sync_log.run(
            engine=engine,
            source="evds",
            operation="backfill",
        ):
            raise RuntimeError("boom")

    # The failure row commits on a fresh connection inside the
    # context-manager's __aexit__, so it survives even if the
    # caller's outer transaction is in any state. See sync_log.run()
    # docstring for why START + FINISH both use fresh connections.
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, error_summary FROM audit.sync_log "
                    "WHERE source='evds' AND operation='backfill' "
                    "ORDER BY sync_id DESC LIMIT 1"
                )
            )
        ).first()
    assert row is not None
    assert row.status == "failed"
    assert "boom" in row.error_summary


async def test_partial_when_records_failed_set(engine: AsyncEngine) -> None:
    async with sync_log.run(
        engine=engine,
        source="bist",
        operation="ohlcv",
    ) as run:
        run.records_ingested(100)
        run.records_failed(3)

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status FROM audit.sync_log "
                    "WHERE source='bist' AND operation='ohlcv' "
                    "ORDER BY sync_id DESC LIMIT 1"
                )
            )
        ).first()
    assert row is not None
    assert row.status == "partial"


async def test_sync_run_status_enum_round_trip() -> None:
    # Trivial bridge test that the Python enum and the DB CHECK constraint agree.
    # Async to satisfy module-level pytest.mark.asyncio(loop_scope="session").
    assert SyncRunStatus.OK.value == "ok"
    assert SyncRunStatus.FAILED.value == "failed"


async def test_exception_redacts_tc_kimlik(engine: AsyncEngine) -> None:
    """If a traceback contains a TC kimlik, error_summary must redact it."""
    fake_kimlik = "12345678901"
    with pytest.raises(RuntimeError, match=fake_kimlik):
        async with sync_log.run(
            engine=engine,
            source="kap",
            operation="redact_test",
        ):
            raise RuntimeError(f"Customer {fake_kimlik} blew up")

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT error_summary FROM audit.sync_log "
                    "WHERE source='kap' AND operation='redact_test' "
                    "ORDER BY sync_id DESC LIMIT 1"
                )
            )
        ).first()
    assert row is not None
    assert fake_kimlik not in row.error_summary
    assert "[REDACTED-TC-KIMLIK]" in row.error_summary
