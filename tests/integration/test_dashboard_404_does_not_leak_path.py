"""404 handler renders a static page that does not echo the path.

Spec §6.3 finding 5: the 404 handler renders a static ``NotFoundVM``
with no caller-supplied path interpolation. Echoing back the
requested path would turn 404 into a self-XSS vector and a side
channel for any data the client included in the URL.

The handler must still go through the central ``render(request, vm)``
helper so the base template (sidebar nav + static-asset references)
wraps the page — operators land on a recognisable surface even when
they typo a route.
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


@pytest.mark.asyncio(loop_scope="session")
async def test_404_returns_404_status(_configured_dashboard: None) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/this-route-does-not-exist")
    assert response.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_404_does_not_echo_requested_path(_configured_dashboard: None) -> None:
    """The page MUST NOT contain the requested path. Even an
    HTML-escaped echo would let an attacker probe for backend
    behavior by varying the URL."""
    sentinel = "leakable-path-sentinel-9b7f"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/{sentinel}")
    assert sentinel not in response.text


@pytest.mark.asyncio(loop_scope="session")
async def test_404_does_not_echo_query_string(_configured_dashboard: None) -> None:
    """Same contract for the query string. A 404 page that bounces
    `?q=...` back to the operator is a reflected-XSS surface even
    if HTML-escaped, because the path is operator-attacker-controlled."""
    sentinel = "leakable-query-sentinel-3c1d"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/missing?q={sentinel}")
    assert sentinel not in response.text


@pytest.mark.asyncio(loop_scope="session")
async def test_404_renders_static_title(_configured_dashboard: None) -> None:
    """The static title proves the handler routed through render(...);
    a future regression that bypasses render and returns a Starlette
    default 404 would lose this title."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/no-such-route")
    assert "Not found" in response.text


@pytest.mark.asyncio(loop_scope="session")
async def test_404_includes_base_template(_configured_dashboard: None) -> None:
    """404 still wraps in the dashboard chrome so operators can
    navigate back to a working page."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/no-such-route")
    body = response.text
    assert "/static/dashboard.css" in body
    assert "/static/htmx.min.js" in body
    assert 'href="/audit"' in body
