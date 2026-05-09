"""Integration tests for the /dq/spot-check pending queue + per-sample
form (GET-only)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import aslan_core.dashboard.pages.dq_spot_check as _dq_spot_check  # noqa: F401
from aslan_core.dashboard.app import app, configure_app
from aslan_core.dq import spot_check

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def _configured_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    configure_app(session_factory=session_factory, redis_client=redis_client)
    yield


@pytest_asyncio.fixture(loop_scope="session")
async def _kap_seeded(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async with session_factory() as s:
        await s.execute(text("CREATE SCHEMA IF NOT EXISTS kap"))
        await s.execute(
            text(
                "CREATE TABLE IF NOT EXISTS kap.disclosures ("
                "  disclosure_id BIGSERIAL PRIMARY KEY, "
                "  event_type TEXT NOT NULL, "
                "  published_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            )
        )
        await s.execute(text("DELETE FROM kap.disclosures"))
        for _ in range(5):
            await s.execute(
                text(
                    "INSERT INTO kap.disclosures(event_type) VALUES ('material_event')"
                )
            )
        await s.execute(text("DELETE FROM audit.spot_check_result"))
        await s.execute(text("DELETE FROM audit.spot_check_sample"))
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.spot_check_result"))
        await s.execute(text("DELETE FROM audit.spot_check_sample"))
        await s.execute(text("DROP TABLE IF EXISTS kap.disclosures"))
        await s.execute(text("DROP SCHEMA IF EXISTS kap CASCADE"))
        await s.commit()


async def test_pending_queue_empty(
    _configured_dashboard: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Empty queue still renders 200."""
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.spot_check_result"))
        await s.execute(text("DELETE FROM audit.spot_check_sample"))
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/spot-check")
    assert resp.status_code == 200
    assert "Data Quality — Spot Check" in resp.text
    assert "No pending samples" in resp.text


async def test_pending_queue_lists_drawn_samples(
    _configured_dashboard: None,
    _kap_seeded: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Drawing 3 samples → queue page shows them."""
    async with session_factory() as s:
        ids = await spot_check.draw_sample(session=s, source="kap", n=3)
        await s.commit()
    assert len(ids) == 3
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/spot-check")
    assert resp.status_code == 200
    text_body = resp.text
    assert "Data Quality — Spot Check" in text_body
    # The summary line includes the pending count.
    assert "3 pending sample(s)" in text_body
    # Each sample's link target is rendered.
    for sid in ids:
        assert str(sid) in text_body


async def test_sample_detail_page_renders(
    _configured_dashboard: None,
    _kap_seeded: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        ids = await spot_check.draw_sample(session=s, source="kap", n=1)
        await s.commit()
    sid = ids[0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/dq/spot-check/{sid}")
    assert resp.status_code == 200
    body = resp.text
    assert "Spot Check" in body
    assert "DB row (canonical)" in body
    assert "Raw bytes / API replay" in body
    # The labelling form is on the page.
    assert 'method="post"' in body
    assert "kap-replay-pending" in body
    assert "dq-spot-check-pending" in body


async def test_sample_detail_404_for_unknown_id(
    _configured_dashboard: None,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/spot-check/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_sample_detail_400_for_bad_uuid(
    _configured_dashboard: None,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/spot-check/not-a-uuid")
    assert resp.status_code == 400
