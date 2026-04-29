"""Smoke-test the ``aslan_core.models.ts`` ORM classes.

These models are PRIVATE — they exist so the writer can use
SQLAlchemy ``select(...)`` / ``insert(...)`` syntax for the
read-back compare path. Public consumers must use
``aslan_core.timeseries.ObservationWriter`` / ``ObservationReader``
and the Pydantic models in ``aslan_core.schemas.timeseries``.

The tests below verify the ORM compiles to SQL that the migrated
schema accepts (column names, types, FKs match) without exercising
the higher-level writer.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.models.ts import Observation, SeriesCatalog, SeriesSubject

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_series_catalog_select_runs(session: AsyncSession) -> None:
    res = await session.execute(select(SeriesCatalog).limit(1))
    _ = res.all()  # must not raise — schema-level smoke


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_select_runs(session: AsyncSession) -> None:
    res = await session.execute(select(Observation).limit(1))
    _ = res.all()


@pytest.mark.asyncio(loop_scope="session")
async def test_series_subject_select_runs(session: AsyncSession) -> None:
    res = await session.execute(select(SeriesSubject).limit(1))
    _ = res.all()


def test_series_catalog_columns_present() -> None:
    """The ORM declares every column the migration creates so
    ``select(SeriesCatalog)`` will not need any raw SQL fallbacks."""
    cols = SeriesCatalog.__table__.columns.keys()
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
    assert expected.issubset(set(cols)), f"missing: {expected - set(cols)}"


def test_observation_columns_present() -> None:
    cols = Observation.__table__.columns.keys()
    expected = {
        "series_id",
        "ts",
        "as_of",
        "value",
        "value_text",
        "quality_flag",
        "ingestion_run_id",
        "payload_hash",
        "metadata",
        "actor_id",
        "actor_kind",
        "client_ip",
        "user_agent",
        "request_id",
    }
    assert expected.issubset(set(cols)), f"missing: {expected - set(cols)}"


def test_series_subject_columns_present() -> None:
    cols = SeriesSubject.__table__.columns.keys()
    expected = {
        "series_id",
        "subject_id",
        "role",
        "actor_id",
        "actor_kind",
        "request_id",
        "created_at",
    }
    assert expected.issubset(set(cols)), f"missing: {expected - set(cols)}"
