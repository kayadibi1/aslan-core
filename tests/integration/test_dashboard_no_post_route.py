"""POST to every registered route returns 405.

Spec §3 + §8.2: the v0.6.0 dashboard is read-only. The unit test
``test_dashboard_routes_are_get_only.py`` enforces this at the
route-table layer; this integration test goes through the full
ASGI stack so a regression that registered a non-routing POST
shim (e.g. a middleware that dispatches POSTs) would still be
caught.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.routing import Route

from aslan_core.dashboard.app import app, configure_app

pytestmark = pytest.mark.integration


def _post_paths() -> list[str]:
    """Walk the route table and pick a representative request path
    for each route. Static-asset routes are kept (their POST is
    just as forbidden as any page's)."""
    paths: list[str] = []
    for route in app.routes:
        if isinstance(route, Route) and route.path:
            # Route paths in this app are literal (no path
            # parameters), so the path itself is a valid request URL.
            paths.append(route.path)
    return paths


@pytest_asyncio.fixture(loop_scope="session")
async def _configured_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    configure_app(session_factory=session_factory, redis_client=redis_client)
    yield


@pytest.mark.parametrize("path", _post_paths())
@pytest.mark.asyncio(loop_scope="session")
async def test_post_to_registered_route_returns_405(
    _configured_dashboard: None,
    path: str,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(path)
    assert response.status_code == 405, (
        f"POST {path!r} returned {response.status_code}; expected 405 — "
        "the v0.6.0 dashboard is read-only at the route-table layer"
    )
