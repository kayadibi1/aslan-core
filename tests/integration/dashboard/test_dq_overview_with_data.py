"""Smoke that /dq/overview renders the heatmap with populated rows."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import aslan_core.dashboard.pages.dq_coverage as _dq_coverage  # noqa: F401
import aslan_core.dashboard.pages.dq_overview as _dq_overview  # noqa: F401
import aslan_core.dashboard.pages.dq_recency as _dq_recency  # noqa: F401
from aslan_core.dashboard.app import app, configure_app
from aslan_core.dq import coverage, recency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def _configured_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    configure_app(session_factory=session_factory, redis_client=redis_client)
    yield


async def test_dq_overview_renders_heatmap_with_data(
    _configured_dashboard: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Seed one recency observation + one coverage snapshot
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.recency_observation"))
        await s.execute(text("DELETE FROM audit.coverage_snapshot"))
        now = datetime.now(UTC)
        await recency.observe(
            session=s,
            source="kap",
            observed_at=now,
            upstream_latest_at=now,
            db_latest_at=now,
            sla_target_seconds=300,
            probe_detail={"test": True},
        )
        await coverage.snapshot(
            session=s,
            source="bist",
            dimension="entity",
            observed_at=now.isoformat(),
            expected_count=502,
            actual_count=500,
            target_pct=99.0,
        )
        await s.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/overview")
    assert resp.status_code == 200
    body = resp.text
    # Heatmap headers
    assert "Data Quality — Overview" in body
    assert "Recency" in body
    assert "Coverage" in body
    # Source rows
    assert "KAP" in body
    assert "BIST" in body
    # State classes
    assert "aslan-heat-" in body


async def test_dq_recency_renders_with_data(
    _configured_dashboard: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.recency_observation"))
        now = datetime.now(UTC)
        await recency.observe(
            session=s,
            source="kap",
            observed_at=now,
            upstream_latest_at=now,
            db_latest_at=now,
            sla_target_seconds=300,
            probe_detail=None,
        )
        await s.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/recency")
    assert resp.status_code == 200
    body = resp.text
    assert "Recency" in body
    assert "Last 1 h" in body or "Last 24 h" in body


async def test_dq_coverage_renders_with_data(
    _configured_dashboard: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.coverage_snapshot"))
        now = datetime.now(UTC)
        await coverage.snapshot(
            session=s,
            source="bist",
            dimension="entity",
            observed_at=now.isoformat(),
            expected_count=502,
            actual_count=500,
            target_pct=99.0,
        )
        await s.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/coverage")
    assert resp.status_code == 200
    body = resp.text
    assert "Data Quality — Coverage" in body
    assert "BIST" in body
    assert "entity" in body
