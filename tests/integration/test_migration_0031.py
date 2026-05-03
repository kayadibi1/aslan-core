"""Migration 0031 — ts.canonical_financial table."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_ENTITY_ID = "aaaaaaaa-0031-0031-0031-aaaaaaaaaaaa"
_RUN_ID = 99031


async def _seed_fk_deps(session: AsyncSession) -> None:
    """Insert source, ingestion_run and currency rows needed by FK columns."""
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('test0031', 'Test0031', 'scraper', 'open') "
            "ON CONFLICT DO NOTHING"
        )
    )
    await session.execute(
        text(
            "INSERT INTO src.ingestion_run (ingestion_run_id, source_id, job_name, status) "
            "VALUES (:run, 'test0031', 'seed', 'succeeded') "
            "ON CONFLICT DO NOTHING"
        ),
        {"run": _RUN_ID},
    )
    await session.execute(
        text(
            "INSERT INTO ref.currency (currency_code, name) "
            "VALUES ('TRY', 'Turkish Lira') ON CONFLICT DO NOTHING"
        )
    )
    await session.execute(
        text(
            "INSERT INTO ref.entity "
            "(entity_id, entity_type, legal_name, status, source_id, ingestion_run_id) "
            "VALUES (:eid, 'company', 'Test0031 Co.', 'active', 'test0031', :run) "
            "ON CONFLICT DO NOTHING"
        ),
        {"eid": _ENTITY_ID, "run": _RUN_ID},
    )


async def _wipe_test_data(session: AsyncSession) -> None:
    await session.execute(
        text("DELETE FROM ts.canonical_financial WHERE entity_id = :eid"),
        {"eid": _ENTITY_ID},
    )
    await session.execute(
        text("DELETE FROM ref.entity WHERE entity_id = :eid"),
        {"eid": _ENTITY_ID},
    )
    await session.execute(
        text("DELETE FROM src.ingestion_run WHERE ingestion_run_id = :run"),
        {"run": _RUN_ID},
    )
    await session.execute(text("DELETE FROM src.source WHERE source_id = 'test0031'"))
    await session.commit()


# -- Table structure -----------------------------------------------------------


async def test_table_exists_in_ts_schema(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'ts' AND table_name = 'canonical_financial' "
            "ORDER BY ordinal_position"
        )
    )
    columns = [r[0] for r in result.fetchall()]
    assert "entity_id" in columns
    assert "canonical_code" in columns
    assert "period_end" in columns
    assert "period_type" in columns
    assert "consolidation" in columns
    assert "restatement_basis" in columns
    assert "currency_code" in columns
    assert "accounting_standard" in columns
    assert "value" in columns
    assert "payload_hash" in columns
    assert "source_contributions" in columns
    assert "computed" in columns
    assert "quality_flags" in columns
    assert "cpi_base_date" in columns
    assert "measuring_unit_date" in columns
    assert "mapping_version" in columns
    assert "manifest_hash" in columns
    assert "as_of" in columns
    assert "ingestion_run_id" in columns


# -- Primary key ---------------------------------------------------------------


async def test_pk_includes_required_columns(session: AsyncSession) -> None:
    """PK must cover period_type, currency_code, accounting_standard,
    cpi_base_date, and mapping_version in addition to entity/code/period."""
    result = await session.execute(
        text(
            "SELECT a.attname "
            "FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "  AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'ts.canonical_financial'::regclass "
            "  AND i.indisprimary "
            "ORDER BY a.attname"
        )
    )
    pk_cols = {r[0] for r in result.fetchall()}
    assert "period_type" in pk_cols
    assert "currency_code" in pk_cols
    assert "accounting_standard" in pk_cols
    assert "cpi_base_date" in pk_cols
    assert "mapping_version" in pk_cols
    assert "entity_id" in pk_cols
    assert "canonical_code" in pk_cols
    assert "period_end" in pk_cols
    assert "restatement_basis" in pk_cols
    assert "as_of" in pk_cols


# -- Index ---------------------------------------------------------------------


async def test_cf_entity_period_index_exists(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'ts' "
            "  AND tablename = 'canonical_financial' "
            "  AND indexname = 'cf_entity_period'"
        )
    )
    assert result.scalar() == "cf_entity_period"


# -- Dashboard grant -----------------------------------------------------------


async def test_dashboard_can_select_canonical_financial(session: AsyncSession) -> None:
    result = await session.execute(
        text("SELECT has_table_privilege(  'aslan_dashboard', 'ts.canonical_financial', 'SELECT')")
    )
    assert result.scalar() is True


# -- CHECK constraint: restatement_basis values --------------------------------


async def test_period_type_check_rejects_invalid(session: AsyncSession) -> None:
    """period_type CHECK must reject a value not in ('q', 'h', 'y', 'ytd')."""
    await _seed_fk_deps(session)
    await session.commit()

    with pytest.raises(Exception, match=r"check|violates"):
        await session.execute(
            text(
                "INSERT INTO ts.canonical_financial "
                "(entity_id, canonical_code, period_end, period_type, "
                "consolidation, restatement_basis, currency_code, "
                "accounting_standard, payload_hash, source_contributions, "
                "mapping_version, manifest_hash, as_of, ingestion_run_id) "
                "VALUES ("
                ":eid, 'Revenue', '2024-12-31', 'INVALID', "
                "'consolidated', 'as_reported', 'TRY', "
                "'ifrs', :phash, '{}', 1, :mhash, now(), :run)"
            ),
            {
                "eid": _ENTITY_ID,
                "run": _RUN_ID,
                "phash": "a" * 64,
                "mhash": "b" * 64,
            },
        )
        await session.commit()
    await session.rollback()
    await _wipe_test_data(session)


async def test_restatement_basis_check_rejects_invalid(session: AsyncSession) -> None:
    """restatement_basis CHECK must reject values outside the allowed set."""
    await _seed_fk_deps(session)
    await session.commit()

    with pytest.raises(Exception, match=r"check|violates"):
        await session.execute(
            text(
                "INSERT INTO ts.canonical_financial "
                "(entity_id, canonical_code, period_end, period_type, "
                "consolidation, restatement_basis, currency_code, "
                "accounting_standard, payload_hash, source_contributions, "
                "mapping_version, manifest_hash, as_of, ingestion_run_id) "
                "VALUES ("
                ":eid, 'Revenue', '2024-12-31', 'y', "
                "'consolidated', 'nominal', 'TRY', "
                "'ifrs', :phash, '{}', 1, :mhash, now(), :run)"
            ),
            {
                "eid": _ENTITY_ID,
                "run": _RUN_ID,
                "phash": "a" * 64,
                "mhash": "b" * 64,
            },
        )
        await session.commit()
    await session.rollback()
    await _wipe_test_data(session)


# -- CHECK constraint: cpi_normalized sentinel guard --------------------------


async def test_cpi_normalized_with_sentinel_cpi_base_date_rejected(
    session: AsyncSession,
) -> None:
    """cpi_normalized with the sentinel cpi_base_date '9999-12-31' must be rejected."""
    await _seed_fk_deps(session)
    await session.commit()

    with pytest.raises(Exception, match=r"check|violates"):
        await session.execute(
            text(
                "INSERT INTO ts.canonical_financial "
                "(entity_id, canonical_code, period_end, period_type, "
                "consolidation, restatement_basis, currency_code, "
                "accounting_standard, payload_hash, source_contributions, "
                "cpi_base_date, measuring_unit_date, "
                "mapping_version, manifest_hash, as_of, ingestion_run_id) "
                "VALUES ("
                ":eid, 'Revenue', '2024-12-31', 'y', "
                "'consolidated', 'cpi_normalized', 'TRY', "
                "'ifrs', :phash, '{}', "
                "'9999-12-31', NULL, "
                "1, :mhash, now(), :run)"
            ),
            {
                "eid": _ENTITY_ID,
                "run": _RUN_ID,
                "phash": "a" * 64,
                "mhash": "b" * 64,
            },
        )
        await session.commit()
    await session.rollback()
    await _wipe_test_data(session)


async def test_cpi_normalized_with_valid_cpi_base_date_accepted(
    session: AsyncSession,
) -> None:
    """cpi_normalized with a real cpi_base_date and measuring_unit_date is valid."""
    await _seed_fk_deps(session)
    await session.commit()

    await session.execute(
        text(
            "INSERT INTO ts.canonical_financial "
            "(entity_id, canonical_code, period_end, period_type, "
            "consolidation, restatement_basis, currency_code, "
            "accounting_standard, payload_hash, source_contributions, "
            "cpi_base_date, measuring_unit_date, "
            "mapping_version, manifest_hash, as_of, ingestion_run_id) "
            "VALUES ("
            ":eid, 'Revenue', '2024-12-31', 'y', "
            "'consolidated', 'cpi_normalized', 'TRY', "
            "'ifrs', :phash, '{}', "
            "'2022-12-31', '2024-12-31', "
            "1, :mhash, now(), :run)"
        ),
        {
            "eid": _ENTITY_ID,
            "run": _RUN_ID,
            "phash": "a" * 64,
            "mhash": "b" * 64,
        },
    )
    await session.commit()
    await _wipe_test_data(session)


async def test_as_reported_with_sentinel_cpi_base_date_accepted(
    session: AsyncSession,
) -> None:
    """as_reported with the sentinel cpi_base_date '9999-12-31' (default) is valid."""
    await _seed_fk_deps(session)
    await session.commit()

    await session.execute(
        text(
            "INSERT INTO ts.canonical_financial "
            "(entity_id, canonical_code, period_end, period_type, "
            "consolidation, restatement_basis, currency_code, "
            "accounting_standard, payload_hash, source_contributions, "
            "mapping_version, manifest_hash, as_of, ingestion_run_id) "
            "VALUES ("
            ":eid, 'Revenue', '2024-12-31', 'y', "
            "'consolidated', 'as_reported', 'TRY', "
            "'ifrs', :phash, '{}', "
            "1, :mhash, now(), :run)"
        ),
        {
            "eid": _ENTITY_ID,
            "run": _RUN_ID,
            "phash": "a" * 64,
            "mhash": "b" * 64,
        },
    )
    await session.commit()
    await _wipe_test_data(session)
