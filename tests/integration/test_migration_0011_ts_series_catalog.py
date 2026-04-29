"""Migration 0011 — ts.series_catalog reference table.

Verifies the schema, audit columns, indexes, and CHECK constraints.
Composes with the autouse ``_apply_migrations`` fixture from
``tests/integration/conftest.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _ensure_source(session: AsyncSession, source_id: str = "kap") -> None:
    """Seed a row in ``src.source`` so FK-checks in ts.series_catalog
    succeed. Uses ON CONFLICT DO NOTHING to be idempotent — different
    test orderings may have already inserted it."""
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES (:sid, :sid, 'scraper', 'open') ON CONFLICT DO NOTHING"
        ),
        {"sid": source_id},
    )
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_ts_schema_exists(session: AsyncSession) -> None:
    n = await session.scalar(
        text("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='ts'")
    )
    assert n == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_series_catalog_columns(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema='ts' AND table_name='series_catalog'"
            )
        )
    ).all()
    cols = {r.column_name: (r.data_type, r.is_nullable) for r in rows}
    expected = {
        "series_id",
        "series_code",
        "source_id",
        "entity_id",
        "metric",
        "frequency",
        "unit",
        "currency_code",
        "restatement_basis",
        "accounting_standard",
        "consolidation",
        "period_type",
        "description",
        "pii_class",
        "metadata",
        "actor_id",
        "actor_kind",
        "client_ip",
        "user_agent",
        "request_id",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(cols.keys()), f"missing: {expected - cols.keys()}"
    # Audit columns nullable
    for c in ("actor_id", "actor_kind", "client_ip", "user_agent", "request_id"):
        assert cols[c][1] == "YES", f"audit column {c} should be nullable"
    # Required columns NOT NULL
    for c in ("series_code", "source_id", "metric", "frequency", "unit", "pii_class"):
        assert cols[c][1] == "NO", f"required column {c} should be NOT NULL"


@pytest.mark.asyncio(loop_scope="session")
async def test_series_catalog_indexes(session: AsyncSession) -> None:
    rows = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes WHERE schemaname='ts' AND tablename='series_catalog'"
        )
    )
    names = {r.indexname for r in rows}
    for expected in (
        "series_catalog_pkey",
        "series_catalog_entity",
        "series_catalog_source_metric",
        "series_catalog_pii",
    ):
        assert expected in names, f"missing index {expected}; got {names}"


@pytest.mark.asyncio(loop_scope="session")
async def test_series_code_unique(session: AsyncSession) -> None:
    """Inserting two rows with the same series_code must fail."""
    await _ensure_source(session)
    await session.execute(
        text(
            "INSERT INTO ts.series_catalog "
            "(series_code, source_id, metric, frequency, unit) "
            "VALUES ('mig11_dup', 'kap', 'm', '1d', 'TRY')"
        )
    )
    await session.commit()
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO ts.series_catalog "
                "(series_code, source_id, metric, frequency, unit) "
                "VALUES ('mig11_dup', 'kap', 'other', '1h', 'USD')"
            )
        )
        await session.commit()
    await session.rollback()
    # Cleanup so other tests with different fixtures don't trip on
    # the leftover row.
    await session.execute(text("DELETE FROM ts.series_catalog WHERE series_code='mig11_dup'"))
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_frequency_check_rejects_unknown(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO ts.series_catalog "
                "(series_code, source_id, metric, frequency, unit) "
                "VALUES ('mig11_badfreq', 'kap', 'm', 'weekly', 'TRY')"
            )
        )
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_pii_class_check_rejects_unknown(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO ts.series_catalog "
                "(series_code, source_id, metric, frequency, unit, pii_class) "
                "VALUES ('mig11_badpii', 'kap', 'm', '1d', 'TRY', 'public')"
            )
        )
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_pii_index_is_partial(session: AsyncSession) -> None:
    """series_catalog_pii is a partial index on rows where pii_class
    is not 'none' — keeps the index small for the common case."""
    defn = await session.scalar(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname='ts' AND tablename='series_catalog' "
            "AND indexname='series_catalog_pii'"
        )
    )
    assert defn is not None
    assert "WHERE" in defn.upper()
    assert "none" in defn  # the literal predicate value


@pytest.mark.asyncio(loop_scope="session")
async def test_metadata_default_empty_jsonb(session: AsyncSession) -> None:
    """metadata defaults to {} so non-null is enforced from the DB
    side, matching the v0.4 contract."""
    await _ensure_source(session)
    row = (
        await session.execute(
            text(
                "INSERT INTO ts.series_catalog "
                "(series_code, source_id, metric, frequency, unit) "
                "VALUES ('mig11_default_meta', 'kap', 'm', '1d', 'TRY') "
                "RETURNING metadata"
            )
        )
    ).one()
    assert row.metadata == {}
    await session.execute(
        text("DELETE FROM ts.series_catalog WHERE series_code='mig11_default_meta'")
    )
    await session.commit()
