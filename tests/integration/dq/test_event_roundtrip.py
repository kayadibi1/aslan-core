"""Roundtrip test for dq.event.emit()."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aslan_core.dq import event
from aslan_core.dq.types import Severity

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def test_emit_writes_row(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    emitted_at = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC).isoformat()
    async with session_factory() as session:
        event_id = await event.emit(
            session=session,
            event_type="recency_sla_breach",
            emitter="audit-recency-sweep",
            payload={"source": "kap", "lag_seconds": 1850, "sla": 300},
            severity=Severity.ERROR,
            emitted_at=emitted_at,
        )
        await session.commit()

    assert event_id is not None
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT event_type, emitter, severity, payload "
                    "FROM audit.event WHERE event_id = :eid"
                ),
                {"eid": event_id},
            )
        ).one()
    assert row.event_type == "recency_sla_breach"
    assert row.emitter == "audit-recency-sweep"
    assert row.severity == "error"
    assert row.payload == {"source": "kap", "lag_seconds": 1850, "sla": 300}


async def test_emit_default_severity_is_null(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        event_id = await event.emit(
            session=session,
            event_type="scorecard_generated",
            emitter="audit-scorecard",
            payload={"week_start": "2026-05-04"},
        )
        await session.commit()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT severity FROM audit.event WHERE event_id = :eid"),
                {"eid": event_id},
            )
        ).one()
    assert row.severity is None
