"""Migration 0066 — ref.calendar_tr table + TR public-holiday seed.

Asserts:

  1. Table exists with the expected minimal columns (the dq probe
     reads ``trade_date`` + ``is_trading_day`` + ``holiday_name``).
  2. ``CREATE SCHEMA IF NOT EXISTS ref`` doesn't break when the
     schema already exists (re-runs are idempotent in spirit; alembic
     normally won't re-run, but the SQL must tolerate it).
  3. ``ON CONFLICT (trade_date) DO NOTHING`` does not corrupt rows
     pre-seeded by an earlier migration / hand-loaded by an operator.
  4. The seed lands the documented number of holiday rows.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


# 13 in 2026 + 14 in 2027 (2027 has Eid al-Adha eve as a separate row
# vs. 2026 where the eve fell on Apr 29). See the migration source for
# the full date list.
_EXPECTED_HOLIDAY_COUNT = 13 + 14


async def test_calendar_tr_table_exists_with_expected_columns(
    engine: AsyncEngine,
) -> None:
    """The dq.probes.bist module reads trade_date + is_trading_day."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'ref' AND table_name = 'calendar_tr' "
                    "ORDER BY column_name"
                )
            )
        ).all()
    cols = {r.column_name: r.data_type for r in rows}
    assert "trade_date" in cols
    assert "is_trading_day" in cols
    assert "holiday_name" in cols
    assert cols["trade_date"] == "date"
    assert cols["is_trading_day"] == "boolean"


async def test_calendar_tr_create_schema_if_not_exists_safe(
    engine: AsyncEngine,
) -> None:
    """CREATE SCHEMA IF NOT EXISTS ref doesn't raise when ref already
    exists. The migration sets up the schema first and we want the SQL
    to be re-runnable in spirit (alembic won't re-run a migration, but
    a manual SQL replay must tolerate a pre-existing schema)."""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS ref"))
    # No assertion needed — a raise above would fail the test.


async def test_calendar_tr_seed_holiday_count(engine: AsyncEngine) -> None:
    """The seed lands the documented holiday count."""
    async with engine.connect() as conn:
        n = (
            await conn.execute(
                text("SELECT count(*)::int FROM ref.calendar_tr WHERE is_trading_day = false")
            )
        ).scalar_one()
    assert n == _EXPECTED_HOLIDAY_COUNT, f"expected {_EXPECTED_HOLIDAY_COUNT} holiday rows, got {n}"


async def test_calendar_tr_on_conflict_do_nothing_does_not_corrupt(
    engine: AsyncEngine,
) -> None:
    """Re-INSERT of a seeded date with different holiday_name leaves
    the row unchanged (ON CONFLICT DO NOTHING semantics)."""
    target = date(2026, 1, 1)  # New Year, seeded by 0066
    async with engine.begin() as conn:
        before = (
            await conn.execute(
                text("SELECT holiday_name FROM ref.calendar_tr WHERE trade_date = :d"),
                {"d": target},
            )
        ).scalar_one()
        # Try to re-insert with a different holiday_name; ON CONFLICT
        # DO NOTHING keeps the original row.
        await conn.execute(
            text(
                "INSERT INTO ref.calendar_tr "
                "  (trade_date, is_trading_day, holiday_name) "
                "VALUES (:d, false, 'WRONG_NAME') "
                "ON CONFLICT (trade_date) DO NOTHING"
            ),
            {"d": target},
        )
        after = (
            await conn.execute(
                text("SELECT holiday_name FROM ref.calendar_tr WHERE trade_date = :d"),
                {"d": target},
            )
        ).scalar_one()
    assert after == before
    assert after != "WRONG_NAME"
