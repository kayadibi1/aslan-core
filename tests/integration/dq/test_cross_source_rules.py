"""Tests for the 6 cross-source consistency rules in dq.cross_source.

The source-specific schemas (kap, bist, tefas, mkk, evds) don't exist
on this branch; each rule has an information_schema guard and emits
``xs_rule_skipped`` when its dependencies are missing. We exercise:

  * The skip path for each of the 6 rules (no source tables present).
  * One synthetic-fixture path for ``xs_tefas_holding_dangling_entity``
    (we materialise tefas.fund_holding + a dangling entity_id).
  * One synthetic-fixture path for ``xs_kap_filing_count_recon``
    (placeholder rule that compares against trailing-7d mean).
  * The all-rules driver produces a {rule_name: count} summary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.dq import cross_source

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def _count_skip_events(s: AsyncSession, rule_name: str) -> int:
    row = (
        await s.execute(
            text(
                "SELECT count(*)::int AS n FROM audit.event "
                "WHERE event_type = 'xs_rule_skipped' "
                "  AND payload->>'rule_name' = :rn"
            ),
            {"rn": rule_name},
        )
    ).one()
    return int(row.n)


async def test_all_rules_skip_when_source_tables_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With none of the source schemas present, every rule emits an
    ``xs_rule_skipped`` event and returns no failures."""
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.validation_failure"))
        for rule in cross_source.ALL_RULES:
            results = await rule.runner(session=s)
            assert results == []
            assert await _count_skip_events(s, rule.name) >= 1
        await s.commit()


async def test_run_all_returns_per_rule_count(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        summary = await cross_source.run_all(session=s)
    assert set(summary.keys()) == {r.name for r in cross_source.ALL_RULES}
    for v in summary.values():
        assert v >= 0


# ── Rule 1: dangling-entity fixture ──────────────────────────────


@pytest_asyncio.fixture(loop_scope="session")
async def _tefas_holding_seeded(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Stand up a minimal tefas.fund_holding + insert a row whose
    entity_id is not present in ref.entity. The rule should fire once."""
    async with session_factory() as s:
        await s.execute(text("CREATE SCHEMA IF NOT EXISTS tefas"))
        await s.execute(
            text(
                "CREATE TABLE IF NOT EXISTS tefas.fund_holding ("
                "  fund_id UUID NOT NULL, "
                "  entity_id UUID NOT NULL, "
                "  snapshot_date DATE NOT NULL, "
                "  units NUMERIC, "
                "  PRIMARY KEY (fund_id, entity_id, snapshot_date)"
                ")"
            )
        )
        await s.execute(text("DELETE FROM tefas.fund_holding"))
        # Use a UUID that does not appear in ref.entity
        await s.execute(
            text(
                "INSERT INTO tefas.fund_holding(fund_id, entity_id, snapshot_date, units) "
                "VALUES (gen_random_uuid(), 'deadbeef-dead-beef-dead-beefdeadbeef', "
                "        current_date, 100)"
            )
        )
        await s.execute(text("DELETE FROM audit.validation_failure"))
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.validation_failure"))
        await s.execute(text("DROP TABLE IF EXISTS tefas.fund_holding"))
        await s.execute(text("DROP SCHEMA IF EXISTS tefas CASCADE"))
        await s.commit()


async def test_dangling_holding_entity_fires(
    _tefas_holding_seeded: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        results = await cross_source.xs_tefas_holding_dangling_entity(session=s)
        await s.commit()
    assert len(results) == 1
    assert results[0].rule_name == "xs_tefas_holding_dangling_entity"
    assert results[0].record_pk["entity_id"] == "deadbeef-dead-beef-dead-beefdeadbeef"
    # Persisted to audit.validation_failure
    async with session_factory() as s:
        n = (
            await s.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.validation_failure "
                    "WHERE rule_name = 'xs_tefas_holding_dangling_entity'"
                )
            )
        ).scalar_one()
        assert int(n) == 1


# ── Rule 6: KAP filing-count-recon (placeholder rule) ─────────────


@pytest_asyncio.fixture(loop_scope="session")
async def _kap_disclosures_seeded(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Stand up a minimal kap.disclosures with one anomalous day so the
    placeholder rule fires."""
    async with session_factory() as s:
        await s.execute(text("CREATE SCHEMA IF NOT EXISTS kap"))
        await s.execute(
            text(
                "CREATE TABLE IF NOT EXISTS kap.disclosures ("
                "  disclosure_id BIGSERIAL PRIMARY KEY, "
                "  entity_id UUID, "
                "  category TEXT, "
                "  published_at TIMESTAMPTZ NOT NULL"
                ")"
            )
        )
        await s.execute(text("DELETE FROM kap.disclosures"))
        # 5 days @ 10 filings, 1 day @ 100 filings → trailing mean ~25
        # so the 100-day diverges from the mean and fires.
        base = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        for d in range(1, 6):
            for _ in range(10):
                await s.execute(
                    text(
                        "INSERT INTO kap.disclosures(category, published_at) "
                        "VALUES ('material_event', :ts)"
                    ),
                    {"ts": base - timedelta(days=d)},
                )
        for _ in range(100):
            await s.execute(
                text(
                    "INSERT INTO kap.disclosures(category, published_at) "
                    "VALUES ('material_event', :ts)"
                ),
                {"ts": base - timedelta(days=6)},
            )
        await s.execute(text("DELETE FROM audit.validation_failure"))
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.validation_failure"))
        await s.execute(text("DROP TABLE IF EXISTS kap.disclosures"))
        await s.execute(text("DROP SCHEMA IF EXISTS kap CASCADE"))
        await s.commit()


async def test_kap_filing_count_recon_fires_on_anomaly(
    _kap_disclosures_seeded: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        results = await cross_source.xs_kap_filing_count_recon(session=s)
        await s.commit()
    # At least the 100-filing day should diverge from the mean.
    assert len(results) >= 1
    assert all(r.rule_name == "xs_kap_filing_count_recon" for r in results)
    # Placeholder marker event was emitted
    async with session_factory() as s:
        n = (
            await s.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.event "
                    "WHERE event_type = 'kap_api_count_unimplemented'"
                )
            )
        ).scalar_one()
        assert int(n) >= 1


# ── Rule 5: EVDS calendar miss fixture ────────────────────────────


@pytest_asyncio.fixture(loop_scope="session")
async def _evds_calendar_seeded(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Insert one calendar row whose grace window has elapsed and a
    sibling evds.observation table that is empty for the matching
    series_code. The rule should fire."""
    async with session_factory() as s:
        await s.execute(text("CREATE SCHEMA IF NOT EXISTS evds"))
        await s.execute(
            text(
                "CREATE TABLE IF NOT EXISTS evds.observation ("
                "  series_code TEXT NOT NULL, "
                "  observation_date DATE NOT NULL, "
                "  value NUMERIC, "
                "  PRIMARY KEY (series_code, observation_date)"
                ")"
            )
        )
        await s.execute(text("DELETE FROM evds.observation"))
        # Insert a calendar row whose expected_at + grace is in the past
        await s.execute(
            text(
                "INSERT INTO audit.evds_release_calendar(series_code, expected_at, "
                "  grace_seconds) VALUES ('TEST.CAL.MISS', "
                "  now() - INTERVAL '7 days', 3600) "
                "ON CONFLICT DO NOTHING"
            )
        )
        await s.execute(text("DELETE FROM audit.validation_failure"))
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.validation_failure"))
        await s.execute(
            text("DELETE FROM audit.evds_release_calendar WHERE series_code = 'TEST.CAL.MISS'")
        )
        await s.execute(text("DROP TABLE IF EXISTS evds.observation"))
        await s.execute(text("DROP SCHEMA IF EXISTS evds CASCADE"))
        await s.commit()


async def test_evds_observation_calendar_fires_on_miss(
    _evds_calendar_seeded: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        results = await cross_source.xs_evds_observation_calendar(session=s)
        await s.commit()
    matched = [r for r in results if r.record_pk.get("series_code") == "TEST.CAL.MISS"]
    assert len(matched) == 1
    assert matched[0].rule_name == "xs_evds_observation_calendar"
