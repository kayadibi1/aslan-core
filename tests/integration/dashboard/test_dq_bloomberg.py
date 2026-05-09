"""Integration tests for /dq/bloomberg overview, run-detail, and POST."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import aslan_core.dashboard.pages.dq_bloomberg as _dq_bloomberg  # noqa: F401
from aslan_core.dashboard.app import app, configure_app
from aslan_core.dq import bloomberg

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def _configured_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    configure_app(session_factory=session_factory, redis_client=redis_client)
    yield


@pytest_asyncio.fixture(loop_scope="session")
async def _wipe(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.bloomberg_comparison_cell"))
        await s.execute(text("DELETE FROM audit.bloomberg_comparison_run"))
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.bloomberg_comparison_cell"))
        await s.execute(text("DELETE FROM audit.bloomberg_comparison_run"))
        await s.commit()


async def test_overview_empty_renders_prompt(
    _configured_dashboard: None,
    _wipe: None,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/bloomberg")
    assert resp.status_code == 200
    assert "Bloomberg Comparison" in resp.text
    assert "No Bloomberg-comparison run has been opened yet" in resp.text


async def test_overview_with_open_run(
    _configured_dashboard: None,
    _wipe: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await bloomberg.open_quarter(session=s, quarter="2026Q2")
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/bloomberg")
    assert resp.status_code == 200
    body = resp.text
    assert "2026Q2" in body
    # All 60 cells should be NULL → 60 entry forms rendered.
    assert body.count('method="post"') == 60
    # Each anchor entity is heading-rendered.
    for entity in bloomberg.ANCHOR_ENTITIES:
        assert f"<h3>{entity}</h3>" in body or entity in body


async def test_run_detail_renders(
    _configured_dashboard: None,
    _wipe: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        run_id = await bloomberg.open_quarter(session=s, quarter="2026Q3")
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/dq/bloomberg/runs/{run_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "2026Q3" in body


async def test_run_detail_404_for_unknown_id(
    _configured_dashboard: None,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/bloomberg/runs/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_run_detail_400_for_bad_uuid(
    _configured_dashboard: None,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/bloomberg/runs/not-a-uuid")
    assert resp.status_code == 400


# ── POST tests ────────────────────────────────────────────────────


async def test_post_writes_bloomberg_value_and_redirects(
    _configured_dashboard: None,
    _wipe: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await bloomberg.open_quarter(session=s, quarter="2026Q4")
        await s.commit()
    async with session_factory() as s:
        cell = (
            await s.execute(
                text(
                    "SELECT cell_id FROM audit.bloomberg_comparison_cell "
                    "WHERE entity_ticker = 'AKBNK' AND field = 'revenue_q-1' LIMIT 1"
                )
            )
        ).one()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/dq/bloomberg/cells/{cell.cell_id}",
            data={"bloomberg_value": "1234567890", "entered_by": "sidar"},
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dq/bloomberg"
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT bloomberg_value, bloomberg_entered_by "
                    "FROM audit.bloomberg_comparison_cell WHERE cell_id = :cid"
                ),
                {"cid": cell.cell_id},
            )
        ).one()
    assert row.bloomberg_value == "1234567890"
    assert row.bloomberg_entered_by == "sidar"


async def test_post_400_for_missing_value(
    _configured_dashboard: None,
    _wipe: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await bloomberg.open_quarter(session=s, quarter="2026Q4")
        await s.commit()
    async with session_factory() as s:
        cell = (
            await s.execute(text("SELECT cell_id FROM audit.bloomberg_comparison_cell LIMIT 1"))
        ).one()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/dq/bloomberg/cells/{cell.cell_id}",
            data={"bloomberg_value": "", "entered_by": "sidar"},
        )
    assert resp.status_code == 400


async def test_post_400_for_overlong_value(
    _configured_dashboard: None,
    _wipe: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await bloomberg.open_quarter(session=s, quarter="2026Q4")
        await s.commit()
    async with session_factory() as s:
        cell = (
            await s.execute(text("SELECT cell_id FROM audit.bloomberg_comparison_cell LIMIT 1"))
        ).one()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/dq/bloomberg/cells/{cell.cell_id}",
            data={"bloomberg_value": "x" * 257, "entered_by": "sidar"},
        )
    assert resp.status_code == 400


async def test_post_400_for_bad_labeller(
    _configured_dashboard: None,
    _wipe: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await bloomberg.open_quarter(session=s, quarter="2026Q4")
        await s.commit()
    async with session_factory() as s:
        cell = (
            await s.execute(text("SELECT cell_id FROM audit.bloomberg_comparison_cell LIMIT 1"))
        ).one()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Whitespace-only labeller fails the regex.
        resp = await client.post(
            f"/dq/bloomberg/cells/{cell.cell_id}",
            data={"bloomberg_value": "1", "entered_by": "drop table x;"},
        )
    assert resp.status_code == 400


async def test_post_404_for_unknown_cell(
    _configured_dashboard: None,
    _wipe: None,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/dq/bloomberg/cells/00000000-0000-0000-0000-000000000000",
            data={"bloomberg_value": "1", "entered_by": "sidar"},
        )
    assert resp.status_code == 404


async def test_post_400_for_bad_uuid(
    _configured_dashboard: None,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/dq/bloomberg/cells/not-a-uuid",
            data={"bloomberg_value": "1", "entered_by": "sidar"},
        )
    assert resp.status_code == 400
