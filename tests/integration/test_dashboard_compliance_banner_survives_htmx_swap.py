"""Compliance banner survives an htmx-style swap.

Spec §6.2 + §8.2: htmx replaces a sub-tree without re-rendering
the surrounding chrome. If a future htmx partial endpoint emits
markup that does NOT include the banner, an operator who lands on
``/audit`` and then triggers an htmx swap would see the banner
disappear from view. The contract: every page (and every htmx
partial) emits the banner unconditionally.

v0.6.0 has no htmx partial endpoints yet — only the full pages.
This test asserts the contract holds for the full-page response
under the ``HX-Request: true`` header, so when partials land in
v0.7.x the same matching shape covers them.
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


_BANNER_TEXT = "Network-edge logging only — not compliance evidence"


@pytest_asyncio.fixture(loop_scope="session")
async def _configured_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    configure_app(session_factory=session_factory, redis_client=redis_client)
    yield


@pytest.mark.parametrize("path", ["/audit", "/redactions"])
@pytest.mark.asyncio(loop_scope="session")
async def test_banner_survives_htmx_request(
    _configured_dashboard: None,
    path: str,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(path, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert _BANNER_TEXT in response.text


@pytest.mark.parametrize("path", ["/audit", "/redactions"])
@pytest.mark.asyncio(loop_scope="session")
async def test_banner_survives_htmx_request_with_target_header(
    _configured_dashboard: None,
    path: str,
) -> None:
    """A more realistic htmx swap carries an HX-Target header for
    the destination element. The banner contract holds independent
    of which sub-tree the client is replacing."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            path,
            headers={"HX-Request": "true", "HX-Target": "table-body"},
        )
    assert response.status_code == 200
    assert _BANNER_TEXT in response.text
