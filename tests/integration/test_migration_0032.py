"""Migration 0032 — ts.entity_quality_score table."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_ENTITY_ID = "aaaaaaaa-0032-0032-0032-aaaaaaaaaaaa"
_RUN_ID = 99032


async def _seed_fk_deps(session: AsyncSession) -> None:
    """Insert source, ingestion_run and currency rows needed by FK columns."""
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('test0032', 'Test0032', 'scraper', 'open') "
            "ON CONFLICT DO NOTHING"
        )
    )
    await session.execute(
        text(
            "INSERT INTO src.ingestion_run (ingestion_run_id, source_id, job_name, status) "
            "VALUES (:run, 'test0032', 'seed', 'succeeded') "
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
            "VALUES (:eid, 'company', 'Test0032 Co.', 'active', 'test0032', :run) "
            "ON CONFLICT DO NOTHING"
        ),
        {"eid": _ENTITY_ID, "run": _RUN_ID},
    )


async def _wipe_test_data(session: AsyncSession) -> None:
    await session.execute(
        text("DELETE FROM ts.entity_quality_score WHERE entity_id = :eid"),
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
    await session.execute(text("DELETE FROM src.source WHERE source_id = 'test0032'"))
    await session.commit()


# -- Table structure -----------------------------------------------------------


async def test_table_exists_in_ts_schema(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'ts' AND table_name = 'entity_quality_score' "
            "ORDER BY ordinal_position"
        )
    )
    columns = [r[0] for r in result.fetchall()]
    assert "entity_id" in columns
    assert "period_end" in columns
    assert "period_type" in columns
    assert "consolidation" in columns
    assert "currency_code" in columns
    assert "accounting_standard" in columns
    assert "restatement_basis" in columns
    assert "cpi_base_date" in columns
    assert "score" in columns
    assert "insufficient_data" in columns
    assert "checks" in columns
    assert "mapping_version" in columns
    assert "manifest_hash" in columns
    assert "as_of" in columns
    assert "ingestion_run_id" in columns


# -- Primary key ---------------------------------------------------------------


async def test_pk_includes_required_columns(session: AsyncSession) -> None:
    """PK must cover the full cardinality key for quality scores."""
    result = await session.execute(
        text(
            "SELECT a.attname "
            "FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "  AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'ts.entity_quality_score'::regclass "
            "  AND i.indisprimary "
            "ORDER BY a.attname"
        )
    )
    pk_cols = {r[0] for r in result.fetchall()}
    assert "entity_id" in pk_cols
    assert "period_end" in pk_cols
    assert "period_type" in pk_cols
    assert "consolidation" in pk_cols
    assert "currency_code" in pk_cols
    assert "accounting_standard" in pk_cols
    assert "restatement_basis" in pk_cols
    assert "cpi_base_date" in pk_cols
    assert "mapping_version" in pk_cols
    assert "as_of" in pk_cols


# -- Dashboard grant -----------------------------------------------------------


async def test_dashboard_can_select_entity_quality_score(session: AsyncSession) -> None:
    result = await session.execute(
        text("SELECT has_table_privilege(  'aslan_dashboard', 'ts.entity_quality_score', 'SELECT')")
    )
    assert result.scalar() is True


# -- CHECK constraint: score range --------------------------------------------


async def test_score_check_rejects_above_100(session: AsyncSession) -> None:
    """score CHECK must reject values above 100."""
    await _seed_fk_deps(session)
    await session.commit()

    with pytest.raises(Exception, match=r"check|violates"):
        await session.execute(
            text(
                "INSERT INTO ts.entity_quality_score "
                "(entity_id, period_end, period_type, consolidation, "
                "currency_code, accounting_standard, restatement_basis, "
                "score, mapping_version, manifest_hash, as_of, ingestion_run_id) "
                "VALUES ("
                ":eid, '2024-12-31', 'y', 'consolidated', "
                "'TRY', 'ifrs', 'as_reported', "
                "101, 1, :mhash, now(), :run)"
            ),
            {"eid": _ENTITY_ID, "run": _RUN_ID, "mhash": "b" * 64},
        )
        await session.commit()
    await session.rollback()
    await _wipe_test_data(session)


async def test_score_check_rejects_below_0(session: AsyncSession) -> None:
    """score CHECK must reject negative values."""
    await _seed_fk_deps(session)
    await session.commit()

    with pytest.raises(Exception, match=r"check|violates"):
        await session.execute(
            text(
                "INSERT INTO ts.entity_quality_score "
                "(entity_id, period_end, period_type, consolidation, "
                "currency_code, accounting_standard, restatement_basis, "
                "score, mapping_version, manifest_hash, as_of, ingestion_run_id) "
                "VALUES ("
                ":eid, '2024-12-31', 'y', 'consolidated', "
                "'TRY', 'ifrs', 'as_reported', "
                "-1, 1, :mhash, now(), :run)"
            ),
            {"eid": _ENTITY_ID, "run": _RUN_ID, "mhash": "b" * 64},
        )
        await session.commit()
    await session.rollback()
    await _wipe_test_data(session)


async def test_score_check_accepts_boundary_values(session: AsyncSession) -> None:
    """score CHECK must accept 0 and 100 (inclusive boundaries)."""
    await _seed_fk_deps(session)
    await session.commit()

    for score in (0, 100):
        await session.execute(
            text(
                "INSERT INTO ts.entity_quality_score "
                "(entity_id, period_end, period_type, consolidation, "
                "currency_code, accounting_standard, restatement_basis, "
                "score, mapping_version, manifest_hash, as_of, ingestion_run_id) "
                "VALUES ("
                ":eid, '2024-12-31', 'y', 'consolidated', "
                "'TRY', 'ifrs', 'as_reported', "
                ":score, :mv, :mhash, now(), :run)"
            ),
            {
                "eid": _ENTITY_ID,
                "run": _RUN_ID,
                "score": score,
                "mv": score + 1,  # distinct mapping_version to avoid PK clash
                "mhash": "b" * 64,
            },
        )
    await session.commit()
    await _wipe_test_data(session)


# -- CHECK constraint: period_type --------------------------------------------


async def test_period_type_check_rejects_invalid(session: AsyncSession) -> None:
    """period_type CHECK must reject a value not in ('q', 'h', 'y', 'ytd')."""
    await _seed_fk_deps(session)
    await session.commit()

    with pytest.raises(Exception, match=r"check|violates"):
        await session.execute(
            text(
                "INSERT INTO ts.entity_quality_score "
                "(entity_id, period_end, period_type, consolidation, "
                "currency_code, accounting_standard, restatement_basis, "
                "score, mapping_version, manifest_hash, as_of, ingestion_run_id) "
                "VALUES ("
                ":eid, '2024-12-31', 'INVALID', 'consolidated', "
                "'TRY', 'ifrs', 'as_reported', "
                "80, 1, :mhash, now(), :run)"
            ),
            {"eid": _ENTITY_ID, "run": _RUN_ID, "mhash": "b" * 64},
        )
        await session.commit()
    await session.rollback()
    await _wipe_test_data(session)
