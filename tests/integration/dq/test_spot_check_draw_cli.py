"""Integration test for `aslan-core audit spot-check-draw` CLI."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aslan_core.cli.dq import _run_spot_check_draw

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def _patch_engine_spc(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Same engine-redirection pattern as the recency-sweep / coverage-snapshot
    CLI tests. The CLI command would otherwise open its own engine via
    ``create_engine()`` and miss the testcontainer DB."""

    class _NoOpEngine:
        async def dispose(self) -> None:
            return None

    monkeypatch.setattr("aslan_core.cli.dq.create_engine", lambda: _NoOpEngine())
    monkeypatch.setattr(
        "aslan_core.cli.dq.create_session_factory",
        lambda _e: session_factory,
    )
    yield


@pytest_asyncio.fixture(loop_scope="session")
async def _kap_disclosures_seeded_for_cli(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async with session_factory() as s:
        await s.execute(text("CREATE SCHEMA IF NOT EXISTS kap"))
        await s.execute(
            text(
                "CREATE TABLE IF NOT EXISTS kap.disclosures ("
                "  disclosure_id BIGSERIAL PRIMARY KEY, "
                "  event_type TEXT NOT NULL, "
                "  published_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            )
        )
        await s.execute(text("DELETE FROM kap.disclosures"))
        for _ in range(30):
            await s.execute(
                text("INSERT INTO kap.disclosures(event_type) VALUES ('material_event')")
            )
        for _ in range(30):
            await s.execute(
                text("INSERT INTO kap.disclosures(event_type) VALUES ('routine_filing')")
            )
        await s.execute(text("DELETE FROM audit.spot_check_result"))
        await s.execute(text("DELETE FROM audit.spot_check_sample"))
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.spot_check_result"))
        await s.execute(text("DELETE FROM audit.spot_check_sample"))
        await s.execute(text("DROP TABLE IF EXISTS kap.disclosures"))
        await s.execute(text("DROP SCHEMA IF EXISTS kap CASCADE"))
        await s.commit()


async def test_cli_draws_for_kap_only_when_filtered(
    _patch_engine_spc: None,
    _kap_disclosures_seeded_for_cli: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """--source kap --n 5 inserts exactly 5 sample rows for kap, none
    for the other sources."""
    summary = await _run_spot_check_draw(source_filter="kap", n_per_source=5)
    assert summary["drawn"] == 5
    assert summary["sources"] == 1
    async with session_factory() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT source, count(*)::int AS n FROM audit.spot_check_sample GROUP BY source"
                )
            )
        ).all()
    by_source = {r.source: r.n for r in rows}
    assert by_source.get("kap") == 5
    assert by_source.get("bist") is None
    assert by_source.get("evds") is None


async def test_cli_default_all_sources_skips_absent_tables(
    _patch_engine_spc: None,
    _kap_disclosures_seeded_for_cli: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Default (no --source) iterates all 5; only kap lands rows on
    the bare testcontainer (the other 4 puller schemas are absent).
    The skipped sources fire spot_check_draw_skipped events."""
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.spot_check_sample"))
        await s.execute(text("DELETE FROM audit.event WHERE event_type LIKE 'spot_check_%'"))
        await s.commit()
    summary = await _run_spot_check_draw(source_filter=None, n_per_source=3)
    assert summary["drawn"] == 3  # Only kap landed; 4 sources skipped silently.
    assert summary["sources"] == 1
    async with session_factory() as s:
        events = (
            await s.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.event "
                    "WHERE event_type = 'spot_check_draw_skipped'"
                )
            )
        ).scalar_one()
    assert events == 4
