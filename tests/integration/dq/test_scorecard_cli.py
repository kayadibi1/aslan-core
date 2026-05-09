"""Integration tests for ``dq.scorecard.compute`` + ``audit scorecard`` CLI.

Three coverage strands:

  * ``compute`` against a synthetic ``audit.recency_observation`` /
    ``audit.coverage_snapshot`` / ``audit.regression_flag`` /
    ``audit.bloomberg_comparison_cell`` mix — assert the right rows
    land with the right status colours.

  * The CLI roundtrip: invoke ``_run_scorecard`` with ``--week-start``
    against the testcontainer DB, assert ten rows land in
    ``audit.scorecard_snapshot``, an ``audit.event(event_type=
    'scorecard_generated')`` is emitted, and the payload carries the
    rendered ``body_html`` that the dispatcher will hand to the
    email sink.

  * Idempotent re-run: a second invocation for the same week
    overwrites the rows in place (UPSERT) and does NOT change
    ``recorded_at``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aslan_core.cli.dq import _run_scorecard
from aslan_core.dq import scorecard

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


_TEST_WEEK_START = date(2026, 4, 27)  # Monday


@pytest_asyncio.fixture(loop_scope="session")
async def _patch_engine(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Redirect the CLI's create_engine() to the testcontainer engine."""

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
async def _seeded(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Seed audit.* tables for a known week so compute() returns
    deterministic statuses.

    Strategy: insert two recency_observation rows for source='kap'
    (lag 100 + 250 — both well under the 300s p95 target) within the
    week window, and one coverage_snapshot per (kap/filings_today,
    bist/entity) pair. Leaves regression_flag / bloomberg / spot_check
    sources empty — those metrics fall back to the warn/no-data branch.
    """
    week_start_dt = datetime(
        _TEST_WEEK_START.year,
        _TEST_WEEK_START.month,
        _TEST_WEEK_START.day,
        12,
        0,
        tzinfo=UTC,
    )
    async with session_factory() as s:
        # Clean slate for the test week.
        await s.execute(
            text("DELETE FROM audit.scorecard_snapshot WHERE week_start = :w"),
            {"w": _TEST_WEEK_START},
        )
        await s.execute(
            text(
                "DELETE FROM audit.recency_observation WHERE source = 'kap' "
                "  AND observed_at >= :start AND observed_at < :end"
            ),
            {
                "start": week_start_dt,
                "end": week_start_dt + timedelta(days=7),
            },
        )
        await s.execute(
            text(
                "DELETE FROM audit.coverage_snapshot WHERE source = 'kap' "
                "  AND dimension = 'filings_today'"
            )
        )
        await s.execute(
            text(
                "DELETE FROM audit.coverage_snapshot WHERE source = 'bist' "
                "  AND dimension = 'entity'"
            )
        )
        await s.execute(text("DELETE FROM audit.event WHERE event_type = 'scorecard_generated'"))

        # Seed two recency rows (low lags → pass).
        for hours_offset, lag in [(2, 100), (24, 250)]:
            obs_at = week_start_dt + timedelta(hours=hours_offset)
            upstream_at = obs_at - timedelta(seconds=lag)
            db_at = obs_at
            await s.execute(
                text(
                    "INSERT INTO audit.recency_observation("
                    "  source, observed_at, upstream_latest_at, db_latest_at, "
                    "  sla_target_seconds"
                    ") VALUES ('kap', :obs, :up, :db, 300)"
                ),
                {"obs": obs_at, "up": upstream_at, "db": db_at},
            )

        # Seed coverage snapshots: KAP filings_today @ 99.5%, BIST entity @ 100%.
        await s.execute(
            text(
                "INSERT INTO audit.coverage_snapshot("
                "  source, dimension, observed_at, expected_count, "
                "  actual_count, target_pct"
                ") VALUES "
                "  ('kap', 'filings_today', :obs, 200, 199, 99.0), "
                "  ('bist', 'entity', :obs, 502, 502, 100.0)"
            ),
            {"obs": week_start_dt + timedelta(hours=12)},
        )

        await s.commit()
    yield


async def test_compute_returns_ten_rows(
    _seeded: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """compute() returns exactly ten ScorecardRow per spec §5.10."""
    async with session_factory() as s:
        rows = await scorecard.compute(session=s, week_start=_TEST_WEEK_START)
    assert len(rows) == 10
    metric_names = {r.metric_name for r in rows}
    assert metric_names == {
        "kap_recency_p95",
        "kap_recency_p99",
        "evds_freshness_pct",
        "kap_filings_today_coverage",
        "bist_entity_coverage",
        "validation_pass_rate_kap",
        "spot_check_completion_rate_4w",
        "bloomberg_wins_count",
        "regression_flag_open_count",
        "cross_source_rule_clean_count",
    }


async def test_compute_classifies_recency_pass(
    _seeded: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With lag values 100s + 250s, p95 ≤ 300 → pass on the recency row."""
    async with session_factory() as s:
        rows = await scorecard.compute(session=s, week_start=_TEST_WEEK_START)
    by_name = {r.metric_name: r for r in rows}
    p95 = by_name["kap_recency_p95"]
    assert p95.status == "pass", f"expected pass, got {p95!r}"
    assert "s" in p95.actual  # e.g. "242s"


async def test_compute_classifies_coverage_pass(
    _seeded: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """KAP filings_today @ 99.5% and BIST entity @ 100% — both pass."""
    async with session_factory() as s:
        rows = await scorecard.compute(session=s, week_start=_TEST_WEEK_START)
    by_name = {r.metric_name: r for r in rows}
    assert by_name["kap_filings_today_coverage"].status == "pass"
    assert by_name["bist_entity_coverage"].status == "pass"


async def test_cli_writes_rows_and_emits_event(
    _seeded: None,
    _patch_engine: None,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``_run_scorecard`` writes 10 rows and emits scorecard_generated."""
    summary = await _run_scorecard(_TEST_WEEK_START)
    assert summary["rows_written"] == 10
    assert summary["pass_count"] + summary["warn_count"] + summary["fail_count"] == 10

    async with engine.connect() as conn:
        n_rows = (
            await conn.execute(
                text("SELECT count(*)::int FROM audit.scorecard_snapshot WHERE week_start = :w"),
                {"w": _TEST_WEEK_START},
            )
        ).scalar_one()
        assert int(n_rows) == 10

        # The event must be emitted with the rendered body_html in payload.
        ev = (
            await conn.execute(
                text(
                    "SELECT payload FROM audit.event "
                    "WHERE event_type = 'scorecard_generated' "
                    "ORDER BY emitted_at DESC LIMIT 1"
                )
            )
        ).one()
        payload = ev.payload
        if isinstance(payload, str):
            import json as _json

            payload = _json.loads(payload)
        assert payload["week_start"] == _TEST_WEEK_START.isoformat()
        assert payload["rows_written"] == 10
        assert payload["subject"].startswith("[ASLAN AUDIT] Weekly scorecard")
        assert "<table" in payload["body_html"]
        assert "kap_recency_p95" in payload["body_html"]


async def test_cli_idempotent_rerun(
    _seeded: None,
    _patch_engine: None,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second invocation for the same week overwrites the rows in
    place (UPSERT). recorded_at stays at the original insert time
    because the ON CONFLICT clause does not include recorded_at in
    the SET list."""
    await _run_scorecard(_TEST_WEEK_START)
    async with engine.connect() as conn:
        first_row = (
            await conn.execute(
                text(
                    "SELECT recorded_at FROM audit.scorecard_snapshot "
                    "WHERE week_start = :w AND metric_name = 'kap_recency_p95'"
                ),
                {"w": _TEST_WEEK_START},
            )
        ).one()
    # Mutate the underlying data so the new actual differs.
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO audit.recency_observation("
                "  source, observed_at, upstream_latest_at, db_latest_at, "
                "  sla_target_seconds"
                ") VALUES ('kap', :obs, :up, :db, 300)"
            ),
            {
                "obs": datetime(
                    _TEST_WEEK_START.year,
                    _TEST_WEEK_START.month,
                    _TEST_WEEK_START.day,
                    18,
                    0,
                    tzinfo=UTC,
                ),
                "up": datetime(
                    _TEST_WEEK_START.year,
                    _TEST_WEEK_START.month,
                    _TEST_WEEK_START.day,
                    17,
                    59,
                    tzinfo=UTC,
                ),
                "db": datetime(
                    _TEST_WEEK_START.year,
                    _TEST_WEEK_START.month,
                    _TEST_WEEK_START.day,
                    18,
                    0,
                    tzinfo=UTC,
                ),
            },
        )
        await s.commit()

    await _run_scorecard(_TEST_WEEK_START)
    async with engine.connect() as conn:
        n = (
            await conn.execute(
                text("SELECT count(*)::int FROM audit.scorecard_snapshot WHERE week_start = :w"),
                {"w": _TEST_WEEK_START},
            )
        ).scalar_one()
        assert int(n) == 10  # still 10 rows; rewrite in place
        second_row = (
            await conn.execute(
                text(
                    "SELECT recorded_at FROM audit.scorecard_snapshot "
                    "WHERE week_start = :w AND metric_name = 'kap_recency_p95'"
                ),
                {"w": _TEST_WEEK_START},
            )
        ).one()
        assert second_row.recorded_at == first_row.recorded_at
