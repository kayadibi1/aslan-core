"""Integration test for `aslan-core audit coverage-snapshot` CLI."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aslan_core.cli.dq import _run_coverage_snapshot

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def _patch_engine_cov(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Same engine-redirection pattern as the recency-sweep test."""

    class _NoOpEngine:
        async def dispose(self) -> None:
            return None

    monkeypatch.setattr("aslan_core.cli.dq.create_engine", lambda: _NoOpEngine())
    monkeypatch.setattr(
        "aslan_core.cli.dq.create_session_factory",
        lambda _e: session_factory,
    )
    yield


async def test_coverage_snapshot_writes_rows_for_present_tables(
    _patch_engine_cov: None,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ref.entity is present on every test DB (migrations apply); the
    bist.entity probe should write a coverage_snapshot row. ts.canonical_financial
    is also present, so the kap.historical_depth_5y probe writes one too.
    Other probes (kap, evds, tefas) skip on the bare DB."""
    # Clean slate
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.coverage_snapshot"))
        await s.execute(text("DELETE FROM audit.event WHERE event_type LIKE 'coverage_%'"))
        await s.commit()

    summary = await _run_coverage_snapshot()

    # bist.entity + kap.historical_depth_5y always-write tables (ref.entity
    # + ts.canonical_financial). Possibly evds.series and tefas.funds skip.
    assert summary["written"] >= 2
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT source, dimension, expected_count, actual_count "
                    "FROM audit.coverage_snapshot "
                    "ORDER BY source, dimension"
                )
            )
        ).all()
    sources_dimensions = {(r.source, r.dimension) for r in rows}
    assert ("bist", "entity") in sources_dimensions
    assert ("kap", "historical_depth_5y") in sources_dimensions

    # Cleanup
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.coverage_snapshot"))
        await s.execute(text("DELETE FROM audit.event WHERE event_type LIKE 'coverage_%'"))
        await s.commit()


async def test_coverage_snapshot_emits_skipped_event_for_absent_tables(
    _patch_engine_cov: None,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """kap.disclosures and tefas.fund_holding are absent on the bare
    testcontainer; their probes should emit `coverage_probe_skipped`
    events."""
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.event WHERE event_type LIKE 'coverage_%'"))
        await s.commit()

    await _run_coverage_snapshot()

    async with engine.connect() as conn:
        events = (
            await conn.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.event "
                    "WHERE event_type = 'coverage_probe_skipped'"
                )
            )
        ).scalar_one()
    assert events >= 1

    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.coverage_snapshot"))
        await s.execute(text("DELETE FROM audit.event WHERE event_type LIKE 'coverage_%'"))
        await s.commit()
