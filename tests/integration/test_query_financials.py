"""Integration tests for ``aslan_core.query.financials``.

Seeds ``ts.canonical_financial`` rows for two entities with both
restatement bases and exercises the three public query functions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.query.financials import (
    compare_metric,
    get_entity_financials,
    get_metric_timeseries,
)
from aslan_core.query.schemas import (
    AI_PRINCIPAL,
    Consolidation,
    PeriodType,
    RestatementBasis,
)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# IDs scoped to this test file — avoid collisions with other test suites.
# ---------------------------------------------------------------------------

_ENTITY_A = UUID("aaaaaaaa-aa00-aa00-aa00-aaaaaaaa0051")
_ENTITY_B = UUID("bbbbbbbb-bb00-bb00-bb00-bbbbbbbb0051")
_FILING_A = UUID("ffffffff-ff00-ff00-ff00-ffffffffffff")
_RUN_ID = 99051
_SOURCE_ID = "test_qf_0051"
_PHASH = "f" * 64
_MHASH = "e" * 64
_CALLER = AI_PRINCIPAL


# ---------------------------------------------------------------------------
# Seed / wipe helpers
# ---------------------------------------------------------------------------


async def _seed_deps(session: AsyncSession) -> None:
    """Insert FK dependencies: source, ingestion_run, currency, entities."""
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES (:sid, 'TestQF', 'scraper', 'open') ON CONFLICT DO NOTHING"
        ),
        {"sid": _SOURCE_ID},
    )
    await session.execute(
        text(
            "INSERT INTO src.ingestion_run (ingestion_run_id, source_id, job_name, status) "
            "VALUES (:run, :sid, 'seed', 'succeeded') ON CONFLICT DO NOTHING"
        ),
        {"run": _RUN_ID, "sid": _SOURCE_ID},
    )
    await session.execute(
        text(
            "INSERT INTO ref.currency (currency_code, name) "
            "VALUES ('TRY', 'Turkish Lira') ON CONFLICT DO NOTHING"
        )
    )
    for eid, name in [(_ENTITY_A, "Entity A"), (_ENTITY_B, "Entity B")]:
        await session.execute(
            text(
                "INSERT INTO ref.entity "
                "(entity_id, entity_type, legal_name, status, source_id, ingestion_run_id) "
                "VALUES (:eid, 'company', :name, 'active', :sid, :run) "
                "ON CONFLICT DO NOTHING"
            ),
            {"eid": str(eid), "name": name, "sid": _SOURCE_ID, "run": _RUN_ID},
        )
    # financial_line_item rows (for statement_type filter via line_code)
    fli_sql = text(
        "INSERT INTO ts.financial_line_item "
        "(entity_id, filing_id, statement_type, line_code, "
        "period_start, period_end, period_type, value, "
        "currency_code, consolidation, accounting_standard, "
        "as_of, ingestion_run_id) "
        "VALUES (:eid, :fid, :st, :code, "
        ":period_start, :period_end, 'q', 0, "
        "'TRY', 'consolidated', 'ifrs', "
        "now(), :run) "
        "ON CONFLICT DO NOTHING"
    )
    for code, st in [("Revenue", "is"), ("COGS", "is"), ("TotalAssets", "bs")]:
        await session.execute(
            fli_sql,
            {
                "eid": str(_ENTITY_A),
                "fid": str(_FILING_A),
                "st": st,
                "code": code,
                "period_start": date(2024, 10, 1),
                "period_end": date(2024, 12, 31),
                "run": _RUN_ID,
            },
        )
    await session.commit()


async def _seed_financials(session: AsyncSession) -> None:
    """Insert canonical_financial rows for two entities, two periods, two bases."""
    d = date.fromisoformat
    cpi_sentinel = date(9999, 1, 1)
    cpi_base = d("2022-12-31")

    base: dict[str, object] = {
        "consolidation": "consolidated",
        "currency_code": "TRY",
        "accounting_standard": "ifrs",
        "phash": _PHASH,
        "mhash": _MHASH,
        "run": _RUN_ID,
    }

    rows: list[dict[str, object]] = []

    # Entity A — as_reported
    for period_str, rev, cogs, assets in [
        ("2024-12-31", 1000, 400, 5000),
        ("2024-09-30", 900, 360, 4800),
        ("2024-06-30", 850, 340, 4600),
    ]:
        pd = d(period_str)
        for code, val in [("Revenue", rev), ("COGS", cogs), ("TotalAssets", assets)]:
            rows.append(
                {
                    **base,
                    "eid": str(_ENTITY_A),
                    "code": code,
                    "period_end": pd,
                    "period_type": "q",
                    "basis": "as_reported",
                    "value": val,
                    "cpi_base_date": cpi_sentinel,
                    "measuring_unit_date": None,
                    "mapping_version": 1,
                }
            )

    # Entity A — cpi_normalized (two periods)
    for period_str, rev, cogs, assets in [
        ("2024-12-31", 1050, 420, 5250),
        ("2024-09-30", 945, 378, 5040),
    ]:
        pd = d(period_str)
        for code, val in [("Revenue", rev), ("COGS", cogs), ("TotalAssets", assets)]:
            rows.append(
                {
                    **base,
                    "eid": str(_ENTITY_A),
                    "code": code,
                    "period_end": pd,
                    "period_type": "q",
                    "basis": "cpi_normalized",
                    "value": val,
                    "cpi_base_date": cpi_base,
                    "measuring_unit_date": pd,
                    "mapping_version": 1,
                }
            )

    # Entity A — a second mapping_version for 2024-12-31 as_reported Revenue
    rows.append(
        {
            **base,
            "eid": str(_ENTITY_A),
            "code": "Revenue",
            "period_end": d("2024-12-31"),
            "period_type": "q",
            "basis": "as_reported",
            "value": 1010,
            "cpi_base_date": cpi_sentinel,
            "measuring_unit_date": None,
            "mapping_version": 2,
        }
    )

    # Entity B — as_reported only
    for period_str, rev in [("2024-12-31", 2000), ("2024-09-30", 1800)]:
        rows.append(
            {
                **base,
                "eid": str(_ENTITY_B),
                "code": "Revenue",
                "period_end": d(period_str),
                "period_type": "q",
                "basis": "as_reported",
                "value": rev,
                "cpi_base_date": cpi_sentinel,
                "measuring_unit_date": None,
                "mapping_version": 1,
            }
        )

    insert_sql = text(
        "INSERT INTO ts.canonical_financial "
        "(entity_id, canonical_code, period_end, period_type, "
        "consolidation, restatement_basis, currency_code, "
        "accounting_standard, value, payload_hash, source_contributions, "
        "cpi_base_date, measuring_unit_date, "
        "mapping_version, manifest_hash, as_of, ingestion_run_id) "
        "VALUES ("
        ":eid, :code, :period_end, :period_type, "
        ":consolidation, :basis, :currency_code, "
        ":accounting_standard, :value, :phash, '{}', "
        ":cpi_base_date, :measuring_unit_date, "
        ":mapping_version, :mhash, now(), :run)"
    )

    for r in rows:
        await session.execute(insert_sql, r)

    # Quality score for Entity A, 2024-12-31, as_reported
    await session.execute(
        text(
            "INSERT INTO ts.entity_quality_score "
            "(entity_id, period_end, period_type, consolidation, "
            "currency_code, accounting_standard, restatement_basis, "
            "score, mapping_version, manifest_hash, as_of, ingestion_run_id) "
            "VALUES (:eid, :period_end, 'q', 'consolidated', "
            "'TRY', 'ifrs', 'as_reported', "
            "85, 2, :mhash, now(), :run)"
        ),
        {
            "eid": str(_ENTITY_A),
            "period_end": d("2024-12-31"),
            "mhash": _MHASH,
            "run": _RUN_ID,
        },
    )

    await session.commit()


async def _wipe(session: AsyncSession) -> None:
    """Remove all seeded data — reverse FK order."""
    await session.execute(
        text("DELETE FROM ts.entity_quality_score WHERE entity_id IN (:a, :b)"),
        {"a": str(_ENTITY_A), "b": str(_ENTITY_B)},
    )
    await session.execute(
        text("DELETE FROM ts.canonical_financial WHERE entity_id IN (:a, :b)"),
        {"a": str(_ENTITY_A), "b": str(_ENTITY_B)},
    )
    await session.execute(
        text("DELETE FROM ts.financial_line_item WHERE entity_id IN (:a, :b)"),
        {"a": str(_ENTITY_A), "b": str(_ENTITY_B)},
    )
    await session.execute(
        text("DELETE FROM ref.entity WHERE entity_id IN (:a, :b)"),
        {"a": str(_ENTITY_A), "b": str(_ENTITY_B)},
    )
    await session.execute(
        text("DELETE FROM src.ingestion_run WHERE ingestion_run_id = :run"),
        {"run": _RUN_ID},
    )
    await session.execute(
        text("DELETE FROM src.source WHERE source_id = :sid"),
        {"sid": _SOURCE_ID},
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _seed_and_cleanup(session: AsyncSession) -> AsyncIterator[None]:
    """Seed once per test, wipe after."""
    await _seed_deps(session)
    await _seed_financials(session)
    yield
    await _wipe(session)


# ---------------------------------------------------------------------------
# get_entity_financials
# ---------------------------------------------------------------------------


async def test_get_entity_financials_groups_into_periods(
    session: AsyncSession,
) -> None:
    """Rows are grouped by period; each PeriodData has a lines dict."""
    periods = await get_entity_financials(
        session,
        _CALLER,
        _ENTITY_A,
        period_type=PeriodType.QUARTERLY,
        restatement_basis=RestatementBasis.AS_REPORTED,
        consolidation=Consolidation.CONSOLIDATED,
    )
    assert len(periods) == 3
    # First period (DESC) is 2024-12-31
    latest = periods[0]
    assert str(latest.period_end) == "2024-12-31"
    assert "Revenue" in latest.lines
    assert "COGS" in latest.lines
    assert "TotalAssets" in latest.lines


async def test_get_entity_financials_picks_latest_mapping_version(
    session: AsyncSession,
) -> None:
    """DISTINCT ON must prefer mapping_version=2 over mapping_version=1."""
    periods = await get_entity_financials(
        session,
        _CALLER,
        _ENTITY_A,
        period_type=PeriodType.QUARTERLY,
        restatement_basis=RestatementBasis.AS_REPORTED,
        consolidation=Consolidation.CONSOLIDATED,
    )
    latest = periods[0]
    # mapping_version=2 set Revenue to 1010 for 2024-12-31
    assert latest.lines["Revenue"] == Decimal("1010")


async def test_get_entity_financials_statement_type_filter(
    session: AsyncSession,
) -> None:
    """When statement_type is provided, only matching codes appear."""
    from aslan_core.query.schemas import StatementType

    periods = await get_entity_financials(
        session,
        _CALLER,
        _ENTITY_A,
        statement_type=StatementType.IS,
        period_type=PeriodType.QUARTERLY,
        restatement_basis=RestatementBasis.AS_REPORTED,
        consolidation=Consolidation.CONSOLIDATED,
    )
    for p in periods:
        assert "TotalAssets" not in p.lines  # bs line excluded
        assert "Revenue" in p.lines


async def test_get_entity_financials_quality_score(
    session: AsyncSession,
) -> None:
    """Quality score is joined from ts.entity_quality_score."""
    periods = await get_entity_financials(
        session,
        _CALLER,
        _ENTITY_A,
        period_type=PeriodType.QUARTERLY,
        restatement_basis=RestatementBasis.AS_REPORTED,
        consolidation=Consolidation.CONSOLIDATED,
    )
    latest = periods[0]  # 2024-12-31
    assert latest.quality_score == 85


async def test_get_entity_financials_limit(
    session: AsyncSession,
) -> None:
    """limit caps the number of returned PeriodData objects."""
    periods = await get_entity_financials(
        session,
        _CALLER,
        _ENTITY_A,
        period_type=PeriodType.QUARTERLY,
        restatement_basis=RestatementBasis.AS_REPORTED,
        consolidation=Consolidation.CONSOLIDATED,
        limit=1,
    )
    assert len(periods) == 1
    assert str(periods[0].period_end) == "2024-12-31"


# ---------------------------------------------------------------------------
# compare_metric
# ---------------------------------------------------------------------------


async def test_compare_metric_two_entities(
    session: AsyncSession,
) -> None:
    """Returns a dict keyed by entity_id with MetricPoint lists."""
    result = await compare_metric(
        session,
        _CALLER,
        [_ENTITY_A, _ENTITY_B],
        "Revenue",
        restatement_basis=RestatementBasis.AS_REPORTED,
        period_type=PeriodType.QUARTERLY,
        consolidation=Consolidation.CONSOLIDATED,
    )
    assert _ENTITY_A in result
    assert _ENTITY_B in result
    # Entity A has 3 quarterly as_reported Revenue (v1 + v2, DISTINCT picks v2 for 2024-12-31)
    assert len(result[_ENTITY_A]) == 3
    # Entity B has 2 periods
    assert len(result[_ENTITY_B]) == 2
    # Values are period_end DESC
    assert result[_ENTITY_A][0].period_end > result[_ENTITY_A][-1].period_end


async def test_compare_metric_too_many_entities_raises(
    session: AsyncSession,
) -> None:
    """More than 10 entity_ids raises ValueError."""
    ids = [UUID(f"cccccccc-cc00-cc00-cc00-ccccccccc{i:03d}") for i in range(11)]
    with pytest.raises(ValueError, match="at most 10"):
        await compare_metric(
            session,
            _CALLER,
            ids,
            "Revenue",
            restatement_basis=RestatementBasis.AS_REPORTED,
            period_type=PeriodType.QUARTERLY,
            consolidation=Consolidation.CONSOLIDATED,
        )


async def test_compare_metric_empty_raises(
    session: AsyncSession,
) -> None:
    """Empty entity_ids raises ValueError."""
    with pytest.raises(ValueError, match="at least one"):
        await compare_metric(
            session,
            _CALLER,
            [],
            "Revenue",
            restatement_basis=RestatementBasis.AS_REPORTED,
            period_type=PeriodType.QUARTERLY,
            consolidation=Consolidation.CONSOLIDATED,
        )


async def test_compare_metric_deduplicates(
    session: AsyncSession,
) -> None:
    """Duplicate entity_ids are silently removed and don't cause extra rows."""
    result = await compare_metric(
        session,
        _CALLER,
        [_ENTITY_A, _ENTITY_A, _ENTITY_A],
        "Revenue",
        restatement_basis=RestatementBasis.AS_REPORTED,
        period_type=PeriodType.QUARTERLY,
        consolidation=Consolidation.CONSOLIDATED,
    )
    assert len(result) == 1
    assert _ENTITY_A in result


async def test_compare_metric_limit(
    session: AsyncSession,
) -> None:
    """limit caps the number of MetricPoints per entity."""
    result = await compare_metric(
        session,
        _CALLER,
        [_ENTITY_A],
        "Revenue",
        restatement_basis=RestatementBasis.AS_REPORTED,
        period_type=PeriodType.QUARTERLY,
        consolidation=Consolidation.CONSOLIDATED,
        limit=1,
    )
    assert len(result[_ENTITY_A]) == 1


async def test_compare_metric_missing_entity_returns_empty(
    session: AsyncSession,
) -> None:
    """Entity with no data appears with an empty list."""
    missing = UUID("dddddddd-dd00-dd00-dd00-dddddddd0051")
    result = await compare_metric(
        session,
        _CALLER,
        [missing],
        "Revenue",
        restatement_basis=RestatementBasis.AS_REPORTED,
        period_type=PeriodType.QUARTERLY,
        consolidation=Consolidation.CONSOLIDATED,
    )
    assert result[missing] == []


# ---------------------------------------------------------------------------
# get_metric_timeseries
# ---------------------------------------------------------------------------


async def test_get_metric_timeseries_both_bases(
    session: AsyncSession,
) -> None:
    """restatement_basis=None returns both as_reported and cpi_normalized."""
    points = await get_metric_timeseries(
        session,
        _CALLER,
        _ENTITY_A,
        "Revenue",
        restatement_basis=None,
        period_type=PeriodType.QUARTERLY,
        consolidation=Consolidation.CONSOLIDATED,
    )
    # Entity A has 3 as_reported periods and 2 cpi periods. FULL OUTER JOIN
    # produces 3 rows (2024-12-31 has both, 2024-09-30 has both,
    # 2024-06-30 only has as_reported).
    assert len(points) == 3

    # 2024-12-31 has both
    dec31 = next(p for p in points if str(p.period_end) == "2024-12-31")
    assert dec31.as_reported == Decimal("1010")  # mapping_version 2
    assert dec31.cpi_normalized == Decimal("1050")
    assert dec31.cpi_base_date is not None

    # 2024-06-30 only has as_reported
    jun30 = next(p for p in points if str(p.period_end) == "2024-06-30")
    assert jun30.as_reported == Decimal("850")
    assert jun30.cpi_normalized is None


async def test_get_metric_timeseries_single_as_reported(
    session: AsyncSession,
) -> None:
    """Single basis returns only that column populated."""
    points = await get_metric_timeseries(
        session,
        _CALLER,
        _ENTITY_A,
        "Revenue",
        restatement_basis=RestatementBasis.AS_REPORTED,
        period_type=PeriodType.QUARTERLY,
        consolidation=Consolidation.CONSOLIDATED,
    )
    assert len(points) == 3
    for p in points:
        assert p.as_reported is not None
        assert p.cpi_normalized is None
        assert p.cpi_base_date is None


async def test_get_metric_timeseries_single_cpi_normalized(
    session: AsyncSession,
) -> None:
    """CPI-normalized single basis."""
    points = await get_metric_timeseries(
        session,
        _CALLER,
        _ENTITY_A,
        "Revenue",
        restatement_basis=RestatementBasis.CPI_NORMALIZED,
        period_type=PeriodType.QUARTERLY,
        consolidation=Consolidation.CONSOLIDATED,
    )
    assert len(points) == 2
    for p in points:
        assert p.cpi_normalized is not None
        assert p.as_reported is None
        assert p.cpi_base_date is not None


async def test_get_metric_timeseries_limit(
    session: AsyncSession,
) -> None:
    """limit caps returned points."""
    points = await get_metric_timeseries(
        session,
        _CALLER,
        _ENTITY_A,
        "Revenue",
        restatement_basis=None,
        period_type=PeriodType.QUARTERLY,
        consolidation=Consolidation.CONSOLIDATED,
        limit=1,
    )
    assert len(points) == 1
    assert str(points[0].period_end) == "2024-12-31"
