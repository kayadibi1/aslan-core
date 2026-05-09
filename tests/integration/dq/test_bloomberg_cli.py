"""Integration tests for the bloomberg-* CLI commands.

Patches the engine factory the same way the other dq CLI tests do so
the CLI commands run against the testcontainer DB.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aslan_core.cli.dq import (
    _run_bloomberg_claim_check,
    _run_bloomberg_close_quarter,
    _run_bloomberg_open_quarter,
    _run_bloomberg_render,
    _run_bloomberg_sample,
)
from aslan_core.dq import bloomberg

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def _patch_engine_for_bloomberg(
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


@pytest_asyncio.fixture(loop_scope="session")
async def _wipe(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.bloomberg_comparison_cell"))
        await s.execute(text("DELETE FROM audit.bloomberg_comparison_run"))
        await s.execute(text("DELETE FROM audit.event WHERE event_type LIKE 'bloomberg_%'"))
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.bloomberg_comparison_cell"))
        await s.execute(text("DELETE FROM audit.bloomberg_comparison_run"))
        await s.execute(text("DELETE FROM audit.event WHERE event_type LIKE 'bloomberg_%'"))
        await s.commit()


async def test_open_quarter_cli_creates_run(
    _patch_engine_for_bloomberg: None,
    _wipe: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    summary = await _run_bloomberg_open_quarter("2026Q2")
    assert summary["quarter"] == "2026Q2"
    async with session_factory() as s:
        n = (
            await s.execute(
                text(
                    "SELECT count(*)::int FROM audit.bloomberg_comparison_cell c "
                    "JOIN audit.bloomberg_comparison_run r ON r.run_id = c.run_id "
                    "WHERE r.quarter = '2026Q2'"
                )
            )
        ).scalar_one()
    assert int(n) == 60


async def test_close_quarter_cli_refuses_when_incomplete(
    _patch_engine_for_bloomberg: None,
    _wipe: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _run_bloomberg_open_quarter("2026Q2")
    with pytest.raises(bloomberg.QuarterNotReadyError):
        await _run_bloomberg_close_quarter(quarter="2026Q2", closed_by="test")


async def test_close_quarter_cli_succeeds_when_filled(
    _patch_engine_for_bloomberg: None,
    _wipe: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _run_bloomberg_open_quarter("2026Q2")
    async with session_factory() as s:
        await s.execute(
            text(
                "UPDATE audit.bloomberg_comparison_cell SET "
                "  bloomberg_value = 'x', "
                "  bloomberg_entered_by = 'test', "
                "  bloomberg_entered_at = now()"
            )
        )
        await s.commit()
    await _run_bloomberg_close_quarter(quarter="2026Q2", closed_by="test")
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT closed_at FROM audit.bloomberg_comparison_run WHERE quarter = '2026Q2'"
                )
            )
        ).one()
    assert row.closed_at is not None


async def test_sample_cli_drains_null_aslan_cells(
    _patch_engine_for_bloomberg: None,
    _wipe: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """All 60 cells start with NULL aslan_value -> sampler hits each one once."""
    await _run_bloomberg_open_quarter("2026Q2")
    summary = await _run_bloomberg_sample()
    assert summary["sampled"] == 60
    # All five samplers (4 quarterly numeric + filing_lag + 4 placeholders + ...)
    # — the placeholder count equals at least the dividend / capital-action /
    # material_event_field_count cells per entity (3 fields x 5 entities = 15).
    assert summary["placeholders"] >= 15
    async with session_factory() as s:
        remaining = (
            await s.execute(
                text(
                    "SELECT count(*)::int FROM audit.bloomberg_comparison_cell "
                    "WHERE aslan_value IS NULL AND aslan_sampled_at IS NULL"
                )
            )
        ).scalar_one()
    assert int(remaining) == 0


async def test_claim_check_cli_returns_pr_text(
    _patch_engine_for_bloomberg: None,
    _wipe: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No closed run yet — claim_check still returns a sensible PR block."""
    text_block = await _run_bloomberg_claim_check("revenue_q-1")
    assert "revenue_q-1" in text_block
    assert "No closed Bloomberg-comparison run yet" in text_block


async def test_render_cli_writes_file(
    _patch_engine_for_bloomberg: None,
    _wipe: None,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Open + force-fill + close a run, then render to a tmp file."""
    await _run_bloomberg_open_quarter("2026Q1")
    async with session_factory() as s:
        await s.execute(
            text(
                "UPDATE audit.bloomberg_comparison_cell SET "
                "  bloomberg_value = 'b', aslan_value = 'a', "
                "  aslan_advantage = 'wins', "
                "  bloomberg_entered_by = 'test', "
                "  bloomberg_entered_at = now()"
            )
        )
        await s.commit()
    await _run_bloomberg_close_quarter(quarter="2026Q1", closed_by="test")
    target = tmp_path / "nested" / "deeper" / "bloomberg.md"
    summary = await _run_bloomberg_render(str(target))
    assert summary["wrote"] is True
    assert summary["cells"] == 60
    assert summary["quarter"] == "2026Q1"
    body = target.read_text(encoding="utf-8")
    assert "# Bloomberg vs Aslan" in body
    assert "2026Q1" in body
    # The render CLI must auto-create deep parent directories.
    assert target.exists()


async def test_render_cli_no_run_writes_stub(
    _patch_engine_for_bloomberg: None,
    _wipe: None,
    tmp_path: Path,
) -> None:
    target = tmp_path / "no-run.md"
    summary = await _run_bloomberg_render(str(target))
    assert summary["wrote"] is False
    body = target.read_text(encoding="utf-8")
    assert "No closed Bloomberg-comparison run yet" in body
