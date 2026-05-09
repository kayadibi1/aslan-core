"""Integration test: M0 migrations apply cleanly to a fresh DB.

Uses the project-wide `engine` fixture from
`aslan_core.testing.fixtures`, which spins up a Timescale Postgres
testcontainer and runs `_apply_migrations`. The fixture is shared
session-scope so we just check that our new tables landed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def _table_exists(engine: AsyncEngine, schema: str, table: str) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = :s AND table_name = :t)"
            ),
            {"s": schema, "t": table},
        )
        return bool(result.scalar())


async def test_sync_log_table_exists(engine: AsyncEngine) -> None:
    assert await _table_exists(engine, "audit", "sync_log")


async def test_event_table_exists(engine: AsyncEngine) -> None:
    assert await _table_exists(engine, "audit", "event")


async def test_severity_rule_table_exists(engine: AsyncEngine) -> None:
    assert await _table_exists(engine, "audit", "severity_rule")


async def test_alert_dispatch_table_exists(engine: AsyncEngine) -> None:
    assert await _table_exists(engine, "audit", "alert_dispatch")


async def test_existing_audit_events_unchanged(engine: AsyncEngine) -> None:
    """Sanity: migration 0053 does not stomp the existing
    audit.events / audit.observation_batch_keys (migrations 0010/0014).
    """
    assert await _table_exists(engine, "audit", "events")
    assert await _table_exists(engine, "audit", "observation_batch_keys")


async def test_recency_sla_table_exists(engine: AsyncEngine) -> None:
    assert await _table_exists(engine, "audit", "recency_sla")


async def test_recency_observation_table_exists(engine: AsyncEngine) -> None:
    assert await _table_exists(engine, "audit", "recency_observation")


async def test_evds_release_calendar_table_exists(engine: AsyncEngine) -> None:
    assert await _table_exists(engine, "audit", "evds_release_calendar")


async def test_recency_observation_is_hypertable(engine: AsyncEngine) -> None:
    """audit.recency_observation must be a Timescale hypertable on observed_at
    so chunk-based retention/compression can apply at scale (audit.* writes
    are continuous; sweeps every 5 min × 5 sources = ~525K rows/year).
    """
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM timescaledb_information.hypertables "
                    "WHERE hypertable_schema='audit' AND hypertable_name='recency_observation'"
                )
            )
        ).scalar()
    assert row == 1


async def test_coverage_snapshot_table_exists(engine: AsyncEngine) -> None:
    assert await _table_exists(engine, "audit", "coverage_snapshot")
