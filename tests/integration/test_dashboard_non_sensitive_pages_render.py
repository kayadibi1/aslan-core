"""Each non-sensitive page renders against the testcontainer DB+Redis.

Spec §8.2: GET each page returns 200 with the page's heading and
the base template. POST each page returns 405. Per-page table
shapes are exercised here at smoke-level — deeper sentinel coverage
(forbidden columns never appearing in the rendered HTML) lands in
Task 13.

Six routes covered: ``/`` (overview, in its own file), ``/outbox``,
``/streams``, ``/ingestion``, ``/documents``, ``/timeseries``.
This file covers the latter five.
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


@pytest.mark.parametrize(
    ("path", "heading"),
    [
        ("/outbox", "Outbox"),
        ("/streams", "Streams"),
        ("/ingestion", "Ingestion"),
        ("/documents", "Documents"),
        ("/timeseries", "Timeseries"),
    ],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_page_returns_200(
    _configured_dashboard: None,
    path: str,
    heading: str,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(path)
    assert response.status_code == 200
    body = response.text
    assert f"<h1>{heading}</h1>" in body, f"page heading '{heading}' missing from {path}"


@pytest.mark.parametrize(
    "path",
    ["/outbox", "/streams", "/ingestion", "/documents", "/timeseries"],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_page_includes_base_template(
    _configured_dashboard: None,
    path: str,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(path)
    body = response.text
    assert "/static/dashboard.css" in body
    assert "/static/htmx.min.js" in body
    # Sidebar nav is the same on every page.
    assert 'href="/audit"' in body


@pytest.mark.parametrize(
    "path",
    ["/outbox", "/streams", "/ingestion", "/documents", "/timeseries"],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_page_is_get_only(
    _configured_dashboard: None,
    path: str,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(path)
    assert response.status_code == 405
