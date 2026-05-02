"""``/audit`` and ``/redactions`` bucket under ``<sensitive>``.

Spec §6.3 round-5 + §8.2: a metric observer must not be able to
tell which compliance-sensitive surface an operator hit. Both
routes increment the same ``path="<sensitive>"`` counter time
series; neither contributes a path label that names the route.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.dashboard.app import app, configure_app

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(loop_scope="session")
async def _configured_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    configure_app(session_factory=session_factory, redis_client=redis_client)
    yield


async def _scrape() -> str:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    body: str = response.text
    return body


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_request_increments_sensitive_bucket(
    _configured_dashboard: None,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.get("/audit")
    body = await _scrape()
    assert 'path="<sensitive>"' in body
    assert 'path="/audit"' not in body


@pytest.mark.asyncio(loop_scope="session")
async def test_redactions_request_increments_sensitive_bucket(
    _configured_dashboard: None,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.get("/redactions")
    body = await _scrape()
    assert 'path="<sensitive>"' in body
    assert 'path="/redactions"' not in body


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_request_does_not_bucket_under_sensitive(
    _configured_dashboard: None,
) -> None:
    """Sanity check — the non-sensitive routes preserve their own
    label. If a regression accidentally bucketed every page under
    <sensitive>, this test catches it."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.get("/outbox")
    body = await _scrape()
    assert 'path="/outbox"' in body
