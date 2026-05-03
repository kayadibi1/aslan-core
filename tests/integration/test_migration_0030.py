"""Migration 0030 — restatement basis derivation + measuring_unit_date."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_ENTITY_ID_A = "aaaaaaaa-0030-0030-0030-aaaaaaaaaaaa"
_ENTITY_ID_B = "aaaaaaaa-0030-0030-0030-bbbbbbbbbbbb"


async def _seed_fk_deps(session: AsyncSession, run_id: int) -> None:
    """Insert source, ingestion_run and currency rows needed by FK columns."""
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('test0030', 'Test0030', 'scraper', 'open') "
            "ON CONFLICT DO NOTHING"
        )
    )
    await session.execute(
        text(
            "INSERT INTO src.ingestion_run (ingestion_run_id, source_id, job_name, status) "
            "VALUES (:run, 'test0030', 'seed', 'succeeded') "
            "ON CONFLICT DO NOTHING"
        ),
        {"run": run_id},
    )
    await session.execute(
        text(
            "INSERT INTO ref.currency (currency_code, name) "
            "VALUES ('TRY', 'Turkish Lira') ON CONFLICT DO NOTHING"
        )
    )


async def _seed_entity(session: AsyncSession, entity_id: str, run_id: int) -> None:
    await session.execute(
        text(
            "INSERT INTO ref.entity "
            "(entity_id, entity_type, legal_name, status, source_id, ingestion_run_id) "
            "VALUES (:eid, 'company', 'Test0030 Co.', 'active', 'test0030', :run) "
            "ON CONFLICT DO NOTHING"
        ),
        {"eid": entity_id, "run": run_id},
    )


async def _wipe_test_data(session: AsyncSession) -> None:
    ids = {"a": _ENTITY_ID_A, "b": _ENTITY_ID_B}
    for tbl in (
        "ts.financial_line_item",
        "ref.tas29_exemption",
        "ref.entity",
    ):
        await session.execute(
            text(f"DELETE FROM {tbl} WHERE entity_id IN (:a, :b)"),  # noqa: S608
            ids,
        )
    await session.execute(
        text("DELETE FROM src.ingestion_run WHERE ingestion_run_id IN (99030, 99031)")
    )
    await session.execute(text("DELETE FROM src.source WHERE source_id = 'test0030'"))
    await session.commit()


# -- ref.tas29_exemption -------------------------------------------------------


async def test_exemption_table_exists(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'ref' AND table_name = 'tas29_exemption' "
            "ORDER BY ordinal_position"
        )
    )
    columns = [r[0] for r in result.fetchall()]
    assert "entity_id" in columns
    assert "reason" in columns
    assert "added_at" in columns


async def test_exemption_entity_id_is_pk(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT a.attname "
            "FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "  AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'ref.tas29_exemption'::regclass "
            "  AND i.indisprimary"
        )
    )
    pk_cols = [r[0] for r in result.fetchall()]
    assert pk_cols == ["entity_id"]


async def test_dashboard_can_select_exemption(session: AsyncSession) -> None:
    result = await session.execute(
        text("SELECT has_table_privilege('aslan_dashboard', 'ref.tas29_exemption', 'SELECT')")
    )
    assert result.scalar() is True


# -- New columns on ts.financial_line_item ------------------------------------


async def test_measuring_unit_date_column_exists(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = 'ts' "
            "AND table_name = 'financial_line_item' "
            "AND column_name = 'measuring_unit_date'"
        )
    )
    assert result.scalar() == "date"


async def test_derivation_reason_column_exists(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = 'ts' "
            "AND table_name = 'financial_line_item' "
            "AND column_name = 'derivation_reason'"
        )
    )
    assert result.scalar() == "text"


# -- CHECK constraint on restatement_basis ------------------------------------


async def test_restatement_basis_check_allows_valid_values(
    session: AsyncSession,
) -> None:
    """Insert a row with each allowed restatement_basis value."""
    await _wipe_test_data(session)
    await _seed_fk_deps(session, run_id=99030)
    await _seed_entity(session, _ENTITY_ID_A, run_id=99030)
    await session.commit()

    valid = ["nominal", "as_reported", "restated", "adjusted", "cpi_normalized"]
    for idx, basis in enumerate(valid):
        filing_id = f"bbbbbbbb-0030-0030-0030-{idx:012d}"
        await session.execute(
            text(
                "INSERT INTO ts.financial_line_item "
                "(entity_id, filing_id, statement_type, line_code, "
                "period_start, period_end, period_type, currency_code, "
                "consolidation, accounting_standard, restatement_basis, "
                "as_of, ingestion_run_id) "
                "VALUES ("
                ":entity_id, :filing_id, "
                "'bs', 'test_line', '2024-01-01', '2024-12-31', 'y', "
                "'TRY', 'consolidated', 'ifrs', :basis, now(), 99030)"
            ),
            {
                "entity_id": _ENTITY_ID_A,
                "filing_id": filing_id,
                "basis": basis,
            },
        )
    await session.commit()

    await _wipe_test_data(session)


async def test_restatement_basis_check_rejects_invalid(
    session: AsyncSession,
) -> None:
    """An invalid restatement_basis value must be rejected by CHECK."""
    await _wipe_test_data(session)
    await _seed_fk_deps(session, run_id=99031)
    await _seed_entity(session, _ENTITY_ID_B, run_id=99031)
    await session.commit()

    with pytest.raises(Exception, match=r"check|violates"):
        await session.execute(
            text(
                "INSERT INTO ts.financial_line_item "
                "(entity_id, filing_id, statement_type, line_code, "
                "period_start, period_end, period_type, currency_code, "
                "consolidation, accounting_standard, restatement_basis, "
                "as_of, ingestion_run_id) "
                "VALUES ("
                "'aaaaaaaa-0030-0030-0030-bbbbbbbbbbbb', "
                "'cccccccc-0030-0030-0030-000000000000', "
                "'bs', 'test_line', '2024-01-01', '2024-12-31', 'y', "
                "'TRY', 'consolidated', 'ifrs', 'INVALID_BASIS', now(), 99031)"
            )
        )
        await session.commit()
    await session.rollback()

    await _wipe_test_data(session)


# -- Derivation logic (P0-P4 ruleset) -----------------------------------------
#
# The migration already ran at test-setup time, so we cannot observe its
# data changes on freshly seeded rows.  Instead each test seeds FLI rows
# with derivation_reason IS NULL and restatement_basis = 'nominal' (the
# pre-migration defaults), then replays the same SQL update pattern the
# migration uses, and asserts the result.  Everything runs inside a
# SAVEPOINT so teardown is a simple rollback.


async def _insert_fli(
    session: AsyncSession,
    *,
    entity_id: str,
    filing_id: str,
    line_code: str = "test_line",
    period_end: datetime.date = datetime.date(2024, 12, 31),
    accounting_standard: str = "ifrs",
    run_id: int = 99030,
) -> None:
    """Insert a single FLI row with derivation columns NULL (pre-migration state)."""
    await session.execute(
        text(
            "INSERT INTO ts.financial_line_item "
            "(entity_id, filing_id, statement_type, line_code, "
            "period_start, period_end, period_type, currency_code, "
            "consolidation, accounting_standard, restatement_basis, "
            "as_of, ingestion_run_id) "
            "VALUES ("
            ":entity_id, :filing_id, "
            "'bs', :line_code, '2024-01-01', :period_end, 'y', "
            "'TRY', 'consolidated', :std, 'nominal', now(), :run)"
        ),
        {
            "entity_id": entity_id,
            "filing_id": filing_id,
            "line_code": line_code,
            "period_end": period_end,
            "std": accounting_standard,
            "run": run_id,
        },
    )


_DERIVATION_SQL_BACKFILL_MUD = """
    UPDATE ts.financial_line_item fli
    SET    measuring_unit_date = sub.max_period_end
    FROM (
        SELECT filing_id, MAX(period_end) AS max_period_end
        FROM   ts.financial_line_item
        GROUP  BY filing_id
    ) sub
    WHERE fli.filing_id = sub.filing_id
      AND fli.measuring_unit_date IS NULL
"""

_DERIVATION_SQL_P0 = """
    UPDATE ts.financial_line_item fli
    SET    restatement_basis = 'nominal',
           derivation_reason = 'exemption'
    FROM   ref.tas29_exemption ex
    WHERE  fli.entity_id = ex.entity_id
      AND  fli.derivation_reason IS NULL
"""

_DERIVATION_SQL_P1 = """
    UPDATE ts.financial_line_item fli
    SET    restatement_basis = 'as_reported',
           derivation_reason = 'monetary_gl_line'
    WHERE  fli.derivation_reason IS NULL
      AND  EXISTS (
          SELECT 1 FROM ts.financial_line_item p1
          WHERE  p1.filing_id = fli.filing_id
            AND  p1.line_code = 'ifrs-full_GainsLossesOnNetMonetaryPosition'
      )
"""

_DERIVATION_SQL_P2 = """
    UPDATE ts.financial_line_item fli
    SET    restatement_basis = 'as_reported',
           derivation_reason = 'inflation_tags'
    WHERE  fli.derivation_reason IS NULL
      AND  EXISTS (
          SELECT 1 FROM ts.financial_line_item p2
          WHERE  p2.filing_id = fli.filing_id
            AND  (p2.line_code LIKE 'kap-fr_Inflation%'
                  OR p2.line_code = 'kap-fr_InflationAdjustmentsOnCapital')
      )
"""

_DERIVATION_SQL_P3 = """
    UPDATE ts.financial_line_item fli
    SET    restatement_basis = 'as_reported',
           derivation_reason = 'period_mandate'
    WHERE  fli.derivation_reason IS NULL
      AND  fli.period_end >= '2023-12-31'
      AND  fli.accounting_standard = 'ifrs'
      AND  NOT EXISTS (
          SELECT 1 FROM ref.tas29_exemption ex
          WHERE  ex.entity_id = fli.entity_id
      )
"""

_DERIVATION_SQL_P4 = """
    UPDATE ts.financial_line_item fli
    SET    restatement_basis = 'nominal',
           derivation_reason = 'no_evidence'
    WHERE  fli.derivation_reason IS NULL
"""

_FILING_DERIV = "dddddddd-0030-0030-0030-000000000001"
_FILING_DERIV_2 = "dddddddd-0030-0030-0030-000000000002"
_FILING_DERIV_3 = "dddddddd-0030-0030-0030-000000000003"
_FILING_DERIV_4 = "dddddddd-0030-0030-0030-000000000004"
_FILING_DERIV_5 = "dddddddd-0030-0030-0030-000000000005"


async def test_measuring_unit_date_backfill(session: AsyncSession) -> None:
    """measuring_unit_date = MAX(period_end) per filing_id."""
    await _wipe_test_data(session)
    await _seed_fk_deps(session, run_id=99030)
    await _seed_entity(session, _ENTITY_ID_A, run_id=99030)
    await session.commit()

    # Two rows in the same filing with different period_end
    await _insert_fli(
        session,
        entity_id=_ENTITY_ID_A,
        filing_id=_FILING_DERIV,
        period_end=datetime.date(2024, 6, 30),
    )
    await _insert_fli(
        session,
        entity_id=_ENTITY_ID_A,
        filing_id=_FILING_DERIV,
        line_code="test_line_2",
        period_end=datetime.date(2024, 12, 31),
    )
    await session.commit()

    # Run the backfill SQL
    await session.execute(text(_DERIVATION_SQL_BACKFILL_MUD))
    await session.commit()

    result = await session.execute(
        text(
            "SELECT DISTINCT measuring_unit_date FROM ts.financial_line_item WHERE filing_id = :fid"
        ),
        {"fid": _FILING_DERIV},
    )
    dates = [r[0] for r in result.fetchall()]
    # Both rows must have the MAX period_end
    assert len(dates) == 1
    assert str(dates[0]) == "2024-12-31"

    await _wipe_test_data(session)


async def test_derivation_p1_monetary_gl_line(session: AsyncSession) -> None:
    """P1: filing with monetary GL line -> as_reported + monetary_gl_line."""
    await _wipe_test_data(session)
    await _seed_fk_deps(session, run_id=99030)
    await _seed_entity(session, _ENTITY_ID_A, run_id=99030)
    await session.commit()

    # Normal line
    await _insert_fli(
        session,
        entity_id=_ENTITY_ID_A,
        filing_id=_FILING_DERIV_2,
        line_code="ifrs-full_Revenue",
    )
    # The sentinel monetary GL line in the same filing
    await _insert_fli(
        session,
        entity_id=_ENTITY_ID_A,
        filing_id=_FILING_DERIV_2,
        line_code="ifrs-full_GainsLossesOnNetMonetaryPosition",
    )
    await session.commit()

    # Run P1
    await session.execute(text(_DERIVATION_SQL_P1))
    await session.commit()

    result = await session.execute(
        text(
            "SELECT restatement_basis, derivation_reason "
            "FROM ts.financial_line_item "
            "WHERE filing_id = :fid"
        ),
        {"fid": _FILING_DERIV_2},
    )
    rows = result.fetchall()
    for row in rows:
        assert row[0] == "as_reported"
        assert row[1] == "monetary_gl_line"

    await _wipe_test_data(session)


async def test_derivation_p0_exemption(session: AsyncSession) -> None:
    """P0: Entity in ref.tas29_exemption -> nominal + exemption (blocks P3)."""
    await _wipe_test_data(session)
    await _seed_fk_deps(session, run_id=99030)
    await _seed_entity(session, _ENTITY_ID_B, run_id=99030)
    await session.commit()

    # Register entity B as exempt
    await session.execute(
        text(
            "INSERT INTO ref.tas29_exemption (entity_id, reason) "
            "VALUES (:eid, 'voluntary opt-out') ON CONFLICT DO NOTHING"
        ),
        {"eid": _ENTITY_ID_B},
    )

    # Seed an FLI row that *would* match P3 (IFRS, period_end >= 2023-12-31)
    await _insert_fli(
        session,
        entity_id=_ENTITY_ID_B,
        filing_id=_FILING_DERIV_4,
        line_code="ifrs-full_Revenue",
        period_end=datetime.date(2024, 12, 31),
        accounting_standard="ifrs",
    )
    await session.commit()

    # Run the full cascade — P0 must fire first and block P3
    await session.execute(text(_DERIVATION_SQL_P0))
    await session.execute(text(_DERIVATION_SQL_P1))
    await session.execute(text(_DERIVATION_SQL_P2))
    await session.execute(text(_DERIVATION_SQL_P3))
    await session.execute(text(_DERIVATION_SQL_P4))
    await session.commit()

    result = await session.execute(
        text(
            "SELECT restatement_basis, derivation_reason "
            "FROM ts.financial_line_item "
            "WHERE filing_id = :fid"
        ),
        {"fid": _FILING_DERIV_4},
    )
    row = result.fetchone()
    assert row is not None
    assert row[0] == "nominal"
    assert row[1] == "exemption"

    await _wipe_test_data(session)


async def test_derivation_p2_inflation_tags(session: AsyncSession) -> None:
    """P2: kap-fr_Inflation* tag, no monetary GL -> as_reported + inflation_tags."""
    await _wipe_test_data(session)
    await _seed_fk_deps(session, run_id=99030)
    await _seed_entity(session, _ENTITY_ID_A, run_id=99030)
    await session.commit()

    # Normal line
    await _insert_fli(
        session,
        entity_id=_ENTITY_ID_A,
        filing_id=_FILING_DERIV_5,
        line_code="ifrs-full_Revenue",
        period_end=datetime.date(2022, 6, 30),
        accounting_standard="local_gaap",
    )
    # Inflation tag line in the same filing (no monetary GL line)
    await _insert_fli(
        session,
        entity_id=_ENTITY_ID_A,
        filing_id=_FILING_DERIV_5,
        line_code="kap-fr_InflationEffectOnOperatingActivities",
        period_end=datetime.date(2022, 6, 30),
        accounting_standard="local_gaap",
    )
    await session.commit()

    # Run P0, P1 (both no-op), then P2
    await session.execute(text(_DERIVATION_SQL_P0))
    await session.execute(text(_DERIVATION_SQL_P1))
    await session.execute(text(_DERIVATION_SQL_P2))
    await session.commit()

    result = await session.execute(
        text(
            "SELECT restatement_basis, derivation_reason "
            "FROM ts.financial_line_item "
            "WHERE filing_id = :fid"
        ),
        {"fid": _FILING_DERIV_5},
    )
    rows = result.fetchall()
    for row in rows:
        assert row[0] == "as_reported"
        assert row[1] == "inflation_tags"

    await _wipe_test_data(session)


async def test_derivation_p3_period_mandate(session: AsyncSession) -> None:
    """P3: IFRS filing with period_end >= 2023-12-31 -> as_reported + period_mandate."""
    await _wipe_test_data(session)
    await _seed_fk_deps(session, run_id=99030)
    await _seed_entity(session, _ENTITY_ID_A, run_id=99030)
    await session.commit()

    await _insert_fli(
        session,
        entity_id=_ENTITY_ID_A,
        filing_id=_FILING_DERIV_2,
        line_code="ifrs-full_Revenue",
        period_end=datetime.date(2024, 12, 31),
        accounting_standard="ifrs",
    )
    await session.commit()

    # Run P0 first (no-op since no exemption), then P1 (no-op since no
    # monetary GL line), P2 (no-op since no inflation tags), then P3.
    await session.execute(text(_DERIVATION_SQL_P0))
    await session.execute(text(_DERIVATION_SQL_P1))
    await session.execute(text(_DERIVATION_SQL_P2))
    await session.execute(text(_DERIVATION_SQL_P3))
    await session.commit()

    result = await session.execute(
        text(
            "SELECT restatement_basis, derivation_reason "
            "FROM ts.financial_line_item "
            "WHERE filing_id = :fid"
        ),
        {"fid": _FILING_DERIV_2},
    )
    row = result.fetchone()
    assert row is not None
    assert row[0] == "as_reported"
    assert row[1] == "period_mandate"

    await _wipe_test_data(session)


async def test_derivation_p4_no_evidence(session: AsyncSession) -> None:
    """P4: no evidence -> nominal + no_evidence."""
    await _wipe_test_data(session)
    await _seed_fk_deps(session, run_id=99030)
    await _seed_entity(session, _ENTITY_ID_A, run_id=99030)
    await session.commit()

    # A row that won't match any of P0-P3 (pre-2023, not IFRS, no tags)
    await _insert_fli(
        session,
        entity_id=_ENTITY_ID_A,
        filing_id=_FILING_DERIV_3,
        line_code="ifrs-full_Revenue",
        period_end=datetime.date(2022, 6, 30),
        accounting_standard="local_gaap",
    )
    await session.commit()

    # Run the full cascade so P4 is the last to fire
    await session.execute(text(_DERIVATION_SQL_P0))
    await session.execute(text(_DERIVATION_SQL_P1))
    await session.execute(text(_DERIVATION_SQL_P2))
    await session.execute(text(_DERIVATION_SQL_P3))
    await session.execute(text(_DERIVATION_SQL_P4))
    await session.commit()

    result = await session.execute(
        text(
            "SELECT restatement_basis, derivation_reason "
            "FROM ts.financial_line_item "
            "WHERE filing_id = :fid"
        ),
        {"fid": _FILING_DERIV_3},
    )
    row = result.fetchone()
    assert row is not None
    assert row[0] == "nominal"
    assert row[1] == "no_evidence"

    await _wipe_test_data(session)
