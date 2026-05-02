"""Migration 0013 — ts.series_subject (codex F4).

Structured Art. 17 deletion path. Composite PK
``(series_id, subject_id, role)``; FK to ``ts.series_catalog`` is
ON DELETE CASCADE so deleting a series tears down its subject links;
``role`` is constrained by a CHECK matching ``SubjectRole`` Literal in
``schemas.timeseries``; the ``series_subject_lookup`` index covers
``(subject_id, role)`` for the deletion-runtime ``subject_id ->
series_id`` reverse lookup.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _ensure_source(session: AsyncSession, source_id: str = "kap") -> None:
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES (:sid, :sid, 'scraper', 'open') ON CONFLICT DO NOTHING"
        ),
        {"sid": source_id},
    )
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_series_subject_columns(session: AsyncSession) -> None:
    rows = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='ts' AND table_name='series_subject'"
        )
    )
    names = {r.column_name for r in rows}
    for col in (
        "series_id",
        "subject_id",
        "role",
        "actor_id",
        "actor_kind",
        "request_id",
        "created_at",
    ):
        assert col in names


@pytest.mark.asyncio(loop_scope="session")
async def test_role_check_rejects_unknown(session: AsyncSession) -> None:
    await _ensure_source(session)
    await session.execute(
        text(
            "INSERT INTO ts.series_catalog (series_code, source_id, metric, frequency, unit) "
            "VALUES ('mig13_role', 'kap', 'm', '1d', 'TRY') ON CONFLICT DO NOTHING"
        )
    )
    sid = await session.scalar(
        text("SELECT series_id FROM ts.series_catalog WHERE series_code='mig13_role'")
    )
    await session.commit()

    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO ts.series_subject (series_id, subject_id, role) "
                "VALUES (:sid, 'p1', 'alien')"
            ),
            {"sid": sid},
        )
        await session.commit()
    await session.rollback()
    # cleanup
    await session.execute(text("DELETE FROM ts.series_catalog WHERE series_code='mig13_role'"))
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_cascade_on_series_delete(session: AsyncSession) -> None:
    """Deleting a series CASCADES to its subjects."""
    await _ensure_source(session)
    await session.execute(
        text(
            "INSERT INTO ts.series_catalog (series_code, source_id, metric, frequency, unit) "
            "VALUES ('mig13_cas', 'kap', 'm', '1d', 'TRY') ON CONFLICT DO NOTHING"
        )
    )
    sid = await session.scalar(
        text("SELECT series_id FROM ts.series_catalog WHERE series_code='mig13_cas'")
    )
    await session.execute(
        text(
            "INSERT INTO ts.series_subject (series_id, subject_id, role) "
            "VALUES (:sid, 'p1', 'executive')"
        ),
        {"sid": sid},
    )
    await session.commit()

    n = await session.scalar(
        text("SELECT COUNT(*) FROM ts.series_subject WHERE series_id = :sid"),
        {"sid": sid},
    )
    assert n == 1

    await session.execute(
        text("DELETE FROM ts.series_catalog WHERE series_id = :sid"),
        {"sid": sid},
    )
    await session.commit()

    n = await session.scalar(
        text("SELECT COUNT(*) FROM ts.series_subject WHERE series_id = :sid"),
        {"sid": sid},
    )
    assert n == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_lookup_index_present(session: AsyncSession) -> None:
    rows = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes WHERE schemaname='ts' AND tablename='series_subject'"
        )
    )
    names = {r.indexname for r in rows}
    assert "series_subject_lookup" in names


@pytest.mark.asyncio(loop_scope="session")
async def test_pk_composite_is_series_subject_role(session: AsyncSession) -> None:
    rows = await session.execute(
        text(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'ts.series_subject'::regclass AND i.indisprimary "
            "ORDER BY array_position(i.indkey, a.attnum)"
        )
    )
    pk_cols = [r.attname for r in rows]
    assert pk_cols == ["series_id", "subject_id", "role"]
