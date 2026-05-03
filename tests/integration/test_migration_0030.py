"""Migration 0030 — restatement basis derivation + measuring_unit_date."""

from __future__ import annotations

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
