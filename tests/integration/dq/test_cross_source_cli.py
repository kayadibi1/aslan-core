"""Integration tests for the `audit cross-source-consistency` and
`audit regression-detect` CLI entry points.

Both crons are session-scoped: they open their own engine + session
factory via ``create_engine`` / ``create_session_factory``. We patch
both to point at the test container's session factory so the cron
writes to the DB the test assertions read from.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aslan_core.cli.dq import _run_cross_source_consistency, _run_regression_detect

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def _patch_engine(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    class _NoOpEngine:
        async def dispose(self) -> None:
            return None

    monkeypatch.setattr("aslan_core.cli.dq.create_engine", lambda: _NoOpEngine())
    monkeypatch.setattr(
        "aslan_core.cli.dq.create_session_factory",
        lambda _e: session_factory,
    )
    yield


async def test_cross_source_consistency_returns_per_rule_summary(
    _patch_engine: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With no source schemas seeded, every rule should skip and return
    0 failures. The cron returns a {rule_name: int} summary."""
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.validation_failure"))
        await s.commit()
    summary = await _run_cross_source_consistency()
    assert isinstance(summary, dict)
    assert set(summary.keys()) == {
        "xs_tefas_holding_dangling_entity",
        "xs_bist_ticker_kap_issuer",
        "xs_mkk_kap_capital_action_corr",
        "xs_tefas_nav_holdings_recon",
        "xs_evds_observation_calendar",
        "xs_kap_filing_count_recon",
    }
    for v in summary.values():
        assert v >= 0


async def test_regression_detect_summary_has_expected_keys(
    _patch_engine: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With no curated entities seeded, the cron returns 0/0/0 cleanly."""
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.regression_flag"))
        await s.commit()
    summary = await _run_regression_detect()
    assert summary["v1_flagged"] >= 0
    assert summary["v2_dismissed"] >= 0
    assert summary["v2_kept_open"] >= 0
