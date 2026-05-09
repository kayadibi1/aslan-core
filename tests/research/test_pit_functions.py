"""Phase 4 — point-in-time SQL function tests.

Drives the PIT functions added in migration 0049
(``ts.observation_at``, ``ts.financial_line_item_at``,
``ts.canonical_financial_at``) directly via SQL against the
testcontainer Postgres. Seeds multi-version rows and asserts the
function returns the right row at each ``as_of``.

Cases (per docs/specs/bitemporal-research-api/TESTPLAN.md):

- TC-001 — single-row entity, exact-match as_of returns that row.
- TC-002 — multi-version entity, picks latest as_of <= requested.
- TC-003 — as_of strictly before any version returns empty.
- TC-005 — exact microsecond boundary returns the row at that as_of.
- TC-006 — microsecond-edge boundary minus 1µs returns the prior row.
- TC-009 — TAS 29 chain: pre-restatement value at older as_of.
- TC-010 — TAS 29 chain: restated value at newer as_of.

Per SCOPE.md D1, D8, D12, D29.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def _seed_pit_world(session: AsyncSession) -> None:
    """Minimum FK chain reused across PIT tests."""
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('pit_test', 'pit_test', 'scraper', 'open') "
            "ON CONFLICT DO NOTHING"
        )
    )
    await session.execute(
        text(
            "INSERT INTO src.ingestion_run "
            "(ingestion_run_id, source_id, job_name, status) "
            "VALUES (777001, 'pit_test', 'pit_seed', 'succeeded') "
            "ON CONFLICT DO NOTHING"
        )
    )
    await session.execute(
        text(
            "INSERT INTO ref.currency (currency_code, name) "
            "VALUES ('TRY', 'Turkish Lira') ON CONFLICT DO NOTHING"
        )
    )
    await session.commit()


async def _make_series(session: AsyncSession, code: str) -> int:
    await session.execute(
        text(
            "INSERT INTO ts.series_catalog "
            "(series_code, source_id, metric, frequency, unit) "
            "VALUES (:c, 'pit_test', 'm', '1d', 'TRY') ON CONFLICT DO NOTHING"
        ),
        {"c": code},
    )
    sid = await session.scalar(
        text("SELECT series_id FROM ts.series_catalog WHERE series_code = :c"),
        {"c": code},
    )
    return int(sid)


async def _make_entity(session: AsyncSession, name: str) -> UUID:
    eid = uuid4()
    await session.execute(
        text(
            "INSERT INTO ref.entity "
            "(entity_id, entity_type, legal_name, status, "
            " source_id, ingestion_run_id) "
            "VALUES (:eid, 'company', :name, 'active', 'pit_test', 777001)"
        ),
        {"eid": eid, "name": name},
    )
    return eid


# ---------------------------------------------------------------------
# ts.observation_at — TC-001..006
# ---------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_at_single_row_exact_as_of(
    session: AsyncSession, _seed_pit_world: None
) -> None:
    """TC-001 — single-row entity, exact as_of returns that row."""
    sid = await _make_series(session, "pit_tc001")
    ts_v = "2024-06-15T13:00:00Z"
    as_of_v = "2024-06-15T13:30:00Z"
    await session.execute(
        text(
            "INSERT INTO ts.observation "
            "(series_id, ts, as_of, value, ingestion_run_id, payload_hash) "
            "VALUES (:sid, :ts, :asof, 42.10, 777001, repeat('a', 64))"
        ),
        {"sid": sid, "ts": ts_v, "asof": as_of_v},
    )
    await session.commit()

    rows = (
        await session.execute(
            text(
                "SELECT value FROM ts.observation_at(:asof) "
                "WHERE series_id = :sid"
            ),
            {"asof": as_of_v, "sid": sid},
        )
    ).all()
    assert len(rows) == 1
    assert float(rows[0].value) == pytest.approx(42.10)

    await session.execute(
        text("DELETE FROM ts.observation WHERE series_id = :sid"),
        {"sid": sid},
    )
    await session.execute(
        text("DELETE FROM ts.series_catalog WHERE series_code = 'pit_tc001'")
    )
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_at_multi_version_picks_latest_le_as_of(
    session: AsyncSession, _seed_pit_world: None
) -> None:
    """TC-002 — multi-version entity, picks latest as_of <= requested.

    Three versions at the same ``ts``; query at as_of between v2 and v3
    must return v2's value.
    """
    sid = await _make_series(session, "pit_tc002")
    ts_v = "2024-08-01T13:00:00Z"
    versions = [
        ("2024-08-01T13:10:00Z", 100.0),
        ("2024-08-02T09:00:00Z", 200.0),  # v2
        ("2024-08-05T11:00:00Z", 300.0),  # v3 (must be invisible)
    ]
    for asof, val in versions:
        await session.execute(
            text(
                "INSERT INTO ts.observation "
                "(series_id, ts, as_of, value, ingestion_run_id, payload_hash) "
                "VALUES (:sid, :ts, :asof, :val, 777001, repeat('b', 64))"
            ),
            {"sid": sid, "ts": ts_v, "asof": asof, "val": val},
        )
    await session.commit()

    # Pick a request as_of between v2 and v3.
    rows = (
        await session.execute(
            text(
                "SELECT value, as_of FROM ts.observation_at(:asof) "
                "WHERE series_id = :sid"
            ),
            {"asof": "2024-08-03T00:00:00Z", "sid": sid},
        )
    ).all()
    assert len(rows) == 1
    assert float(rows[0].value) == pytest.approx(200.0), (
        "PIT must return latest as_of <= requested (v2)"
    )
    # v3's as_of must NOT have been picked.
    assert rows[0].as_of < datetime(2024, 8, 5, tzinfo=timezone.utc)

    await session.execute(
        text("DELETE FROM ts.observation WHERE series_id = :sid"),
        {"sid": sid},
    )
    await session.execute(
        text("DELETE FROM ts.series_catalog WHERE series_code = 'pit_tc002'")
    )
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_at_before_first_version_returns_empty(
    session: AsyncSession, _seed_pit_world: None
) -> None:
    """TC-003 — as_of strictly before any version returns empty."""
    sid = await _make_series(session, "pit_tc003")
    await session.execute(
        text(
            "INSERT INTO ts.observation "
            "(series_id, ts, as_of, value, ingestion_run_id, payload_hash) "
            "VALUES (:sid, '2023-04-01T08:00:00Z', '2023-04-01T08:00:00Z', "
            "        1.0, 777001, repeat('c', 64))"
        ),
        {"sid": sid},
    )
    await session.commit()

    rows = (
        await session.execute(
            text(
                "SELECT value FROM ts.observation_at(:asof) "
                "WHERE series_id = :sid"
            ),
            {"asof": "2023-03-01T00:00:00Z", "sid": sid},
        )
    ).all()
    assert rows == []

    await session.execute(
        text("DELETE FROM ts.observation WHERE series_id = :sid"),
        {"sid": sid},
    )
    await session.execute(
        text("DELETE FROM ts.series_catalog WHERE series_code = 'pit_tc003'")
    )
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_at_microsecond_boundary(
    session: AsyncSession, _seed_pit_world: None
) -> None:
    """TC-005 / TC-006 — microsecond-precision PIT.

    Two rows at ``...654320Z`` and ``...654321Z``. Query at the exact
    newer microsecond returns the newer row; query 1µs earlier returns
    the older. No rounding, no truncation (D12).
    """
    sid = await _make_series(session, "pit_tc005")
    ts_v = "2024-09-10T15:23:11Z"
    older = "2024-09-10T15:23:11.654320Z"
    newer = "2024-09-10T15:23:11.654321Z"
    for i, asof in enumerate((older, newer), start=1):
        await session.execute(
            text(
                "INSERT INTO ts.observation "
                "(series_id, ts, as_of, value, ingestion_run_id, payload_hash) "
                "VALUES (:sid, :ts, :asof, :val, 777001, repeat('d', 64))"
            ),
            {"sid": sid, "ts": ts_v, "asof": asof, "val": float(i)},
        )
    await session.commit()

    # Query at exact newer microsecond.
    rows = (
        await session.execute(
            text(
                "SELECT value FROM ts.observation_at(:asof) "
                "WHERE series_id = :sid"
            ),
            {"asof": newer, "sid": sid},
        )
    ).all()
    assert len(rows) == 1
    assert float(rows[0].value) == pytest.approx(2.0)

    # Query at older microsecond → must see only the older row.
    rows = (
        await session.execute(
            text(
                "SELECT value FROM ts.observation_at(:asof) "
                "WHERE series_id = :sid"
            ),
            {"asof": older, "sid": sid},
        )
    ).all()
    assert len(rows) == 1
    assert float(rows[0].value) == pytest.approx(1.0)

    await session.execute(
        text("DELETE FROM ts.observation WHERE series_id = :sid"),
        {"sid": sid},
    )
    await session.execute(
        text("DELETE FROM ts.series_catalog WHERE series_code = 'pit_tc005'")
    )
    await session.commit()


# ---------------------------------------------------------------------
# ts.canonical_financial_at — TC-009 / TC-010 (TAS 29 chain)
# ---------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_canonical_financial_at_tas29_chain(
    session: AsyncSession, _seed_pit_world: None
) -> None:
    """TC-009 / TC-010 — TAS 29 restatement chain across two as_of.

    Two rows for the same canonical key under distinct restatement_basis
    values: ``as_reported`` written first; the TAS 29 ``cpi_normalized``
    row appended later. PIT at the older as_of returns only the
    as_reported row; PIT at the newer as_of returns BOTH (because each
    restatement_basis is its own logical entity per the PK).
    """
    eid = await _make_entity(session, "PIT TC009 Corp")
    period_end = date(2022, 12, 31)
    payload_hash = "1" * 64
    manifest_hash = "2" * 64

    # Older row: as_reported, value=1000.00, as_of=2023-03-15.
    await session.execute(
        text(
            "INSERT INTO ts.canonical_financial "
            "(entity_id, canonical_code, period_end, period_type, "
            " consolidation, restatement_basis, currency_code, "
            " accounting_standard, value, payload_hash, source_contributions, "
            " mapping_version, manifest_hash, as_of, "
            " ingestion_run_id, cpi_base_date) "
            "VALUES (:eid, 'revenue', :pe, 'y', 'consolidated', "
            "        'as_reported', 'TRY', 'ifrs', 1000.00, :ph, '{}', "
            "        1, :mh, '2023-03-15T09:00:00Z', 777001, "
            "        '9999-01-01')"
        ),
        {"eid": eid, "pe": period_end, "ph": payload_hash, "mh": manifest_hash},
    )
    # Newer row: cpi_normalized restatement, value=1287.45,
    # as_of=2024-02-10. Different restatement_basis → coexists with the
    # older row in the PK.
    await session.execute(
        text(
            "INSERT INTO ts.canonical_financial "
            "(entity_id, canonical_code, period_end, period_type, "
            " consolidation, restatement_basis, currency_code, "
            " accounting_standard, value, payload_hash, source_contributions, "
            " mapping_version, manifest_hash, as_of, "
            " ingestion_run_id, cpi_base_date, measuring_unit_date) "
            "VALUES (:eid, 'revenue', :pe, 'y', 'consolidated', "
            "        'cpi_normalized', 'TRY', 'ifrs', 1287.45, :ph, '{}', "
            "        1, :mh, '2024-02-10T11:00:00Z', 777001, "
            "        '2022-12-31', '2024-02-10')"
        ),
        {"eid": eid, "pe": period_end, "ph": payload_hash, "mh": manifest_hash},
    )
    await session.commit()

    # Pre-restatement query: only the as_reported row visible.
    rows = (
        await session.execute(
            text(
                "SELECT restatement_basis, value FROM "
                "ts.canonical_financial_at(:asof) WHERE entity_id = :eid"
            ),
            {"asof": "2023-12-01T00:00:00Z", "eid": eid},
        )
    ).all()
    assert len(rows) == 1, f"expected only as_reported pre-restatement, got {rows}"
    assert rows[0].restatement_basis == "as_reported"
    assert Decimal(rows[0].value) == Decimal("1000.00")

    # Post-restatement query: both bases visible.
    rows = (
        await session.execute(
            text(
                "SELECT restatement_basis, value FROM "
                "ts.canonical_financial_at(:asof) WHERE entity_id = :eid "
                "ORDER BY restatement_basis"
            ),
            {"asof": "2024-06-01T00:00:00Z", "eid": eid},
        )
    ).all()
    assert {r.restatement_basis for r in rows} == {
        "as_reported",
        "cpi_normalized",
    }
    by_basis = {r.restatement_basis: Decimal(r.value) for r in rows}
    assert by_basis["as_reported"] == Decimal("1000.00")
    assert by_basis["cpi_normalized"] == Decimal("1287.45")

    # Cleanup.
    await session.execute(
        text("DELETE FROM ts.canonical_financial WHERE entity_id = :eid"),
        {"eid": eid},
    )
    await session.execute(
        text("DELETE FROM ref.entity WHERE entity_id = :eid"),
        {"eid": eid},
    )
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_at_amendment_chain_returns_latest_value(
    session: AsyncSession, _seed_pit_world: None
) -> None:
    """TC-002 (interval-flavored) — amendment chain on the same (series, ts).

    Two as_of rows on the same (series_id, ts). Query at as_of between
    v1 and v2 returns v1's value; query at or after v2 returns v2's
    value. Mirrors the load-bearing case for Moat 2 (KAP amendment).
    """
    sid = await _make_series(session, "pit_amend")
    ts_v = "2024-08-01T13:00:00Z"
    initial = ("2024-08-01T11:00:00Z", 1.50)
    amended = ("2024-08-02T14:00:00Z", 1.75)
    for asof, val in (initial, amended):
        await session.execute(
            text(
                "INSERT INTO ts.observation "
                "(series_id, ts, as_of, value, ingestion_run_id, payload_hash) "
                "VALUES (:sid, :ts, :asof, :val, 777001, repeat('e', 64))"
            ),
            {"sid": sid, "ts": ts_v, "asof": asof, "val": val},
        )
    await session.commit()

    # Between v1 and v2 → v1 visible.
    rows = (
        await session.execute(
            text(
                "SELECT value FROM ts.observation_at(:asof) "
                "WHERE series_id = :sid"
            ),
            {"asof": "2024-08-01T20:00:00Z", "sid": sid},
        )
    ).all()
    assert len(rows) == 1
    assert float(rows[0].value) == pytest.approx(1.50)

    # After v2 → v2 visible.
    rows = (
        await session.execute(
            text(
                "SELECT value FROM ts.observation_at(:asof) "
                "WHERE series_id = :sid"
            ),
            {"asof": "2024-08-03T00:00:00Z", "sid": sid},
        )
    ).all()
    assert len(rows) == 1
    assert float(rows[0].value) == pytest.approx(1.75)

    await session.execute(
        text("DELETE FROM ts.observation WHERE series_id = :sid"),
        {"sid": sid},
    )
    await session.execute(
        text("DELETE FROM ts.series_catalog WHERE series_code = 'pit_amend'")
    )
    await session.commit()
