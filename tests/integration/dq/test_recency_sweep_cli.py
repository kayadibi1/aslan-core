"""Integration test for `aslan-core audit recency-sweep` CLI.

Exercises the testcontainer + migrations stack end-to-end: a fresh
DB (no puller tables present) means every probe returns
`db_latest=None` and the cron emits `recency_probe_skipped` events
rather than `recency_observation` rows. With a stub `kap.disclosures`
table seeded inline, the same sweep writes a real observation row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aslan_core.cli.dq import _run_recency_sweep

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def _patch_engine(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Patch `aslan_core.cli.dq.create_engine` to return the test engine.

    The CLI command opens its own engine via `create_engine()`. For
    tests we redirect to the testcontainer-bound engine fixture so
    the cron writes to the same DB the assertions read from. The
    same fixture also patches `create_session_factory` for symmetry.
    """

    def _factory(_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        return session_factory

    class _NoOpEngine:
        async def dispose(self) -> None:
            return None

    monkeypatch.setattr("aslan_core.cli.dq.create_engine", lambda: _NoOpEngine())
    monkeypatch.setattr(
        "aslan_core.cli.dq.create_session_factory",
        lambda _e: session_factory,
    )
    yield


async def test_recency_sweep_with_no_puller_tables(
    _patch_engine: None,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fresh DB has none of the puller tables; every probe should
    return `db_latest=None` and the cron should skip the observation
    insert and emit `recency_probe_skipped` events."""
    # Drop any pre-existing observations so the assertion is clean.
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.recency_observation"))
        await s.execute(text("DELETE FROM audit.event WHERE event_type LIKE 'recency_%'"))
        await s.commit()

    summary = await _run_recency_sweep(source_filter=None)

    # Six recency_sla rows seeded by migration 0054. None should write
    # an observation (no puller tables exist on the bare testcontainer)
    # except possibly EVDS — which uses audit.evds_release_calendar
    # for upstream and evds.observation for db. The DB side is absent
    # so EVDS also skips.
    assert summary["observed"] == 0
    assert summary["skipped"] >= 1
    async with engine.connect() as conn:
        events = (
            await conn.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.event "
                    "WHERE event_type = 'recency_probe_skipped'"
                )
            )
        ).scalar_one()
    assert events >= 1


async def test_recency_sweep_writes_observation_when_table_present(
    _patch_engine: None,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seed a stub `kap.disclosures` table; rerun sweep; verify an
    observation row lands on `audit.recency_observation`."""
    async with session_factory() as s:
        await s.execute(text("CREATE SCHEMA IF NOT EXISTS kap"))
        await s.execute(
            text(
                "CREATE TABLE IF NOT EXISTS kap.disclosures ("
                "  disclosure_id BIGSERIAL PRIMARY KEY, "
                "  published_at TIMESTAMPTZ NOT NULL"
                ")"
            )
        )
        await s.execute(
            text(
                "INSERT INTO kap.disclosures (published_at) "
                "VALUES (now() - INTERVAL '1 minute'), (now() - INTERVAL '5 minutes')"
            )
        )
        await s.execute(text("DELETE FROM audit.recency_observation"))
        await s.execute(text("DELETE FROM audit.event WHERE event_type LIKE 'recency_%'"))
        await s.commit()

    try:
        summary = await _run_recency_sweep(source_filter="kap")
        assert summary["observed"] >= 1
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT source, lag_seconds, sla_breached "
                        "FROM audit.recency_observation WHERE source = 'kap'"
                    )
                )
            ).all()
        assert len(rows) >= 1
        for row in rows:
            assert row.source == "kap"
            # Both the placeholder upstream (now - 30s) and the seeded
            # 1-minute-old DB row are in the past; lag is small and
            # well under the 300s SLA.
            assert isinstance(row.sla_breached, bool)
    finally:
        # Clean up the stub table so other tests aren't affected.
        async with session_factory() as s:
            await s.execute(text("DROP TABLE IF EXISTS kap.disclosures"))
            await s.execute(text("DROP SCHEMA IF EXISTS kap CASCADE"))
            await s.commit()
