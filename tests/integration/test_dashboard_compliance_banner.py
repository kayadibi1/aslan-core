"""Compliance banner enforcement on ``/audit`` and ``/redactions``.

Spec §6.2 + §8.2: the audit and redactions pages render a standing
banner with the literal string ``Network-edge logging only — not
compliance evidence``. The banner is server-rendered by the page
template — no client-side dismissal mechanism (no ``dismiss``,
``data-dismiss``, ``localStorage``, or ``setItem(`` token in the
markup), no environment flag that suppresses it, no cookie that
hides it. Refreshing or htmx-swapping returns the banner.

The banner is a guard against the v0.6.0 audit story being
mistaken for compliance-grade per-view attribution. ``/audit`` and
``/redactions`` are the surfaces where that mistake would matter.
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

_DISMISS_TOKENS = (
    "data-dismiss",
    "localStorage",
    "setItem(",
    'class="dismiss"',
    "dismiss-banner",
)


@pytest_asyncio.fixture(loop_scope="session")
async def _configured_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    configure_app(session_factory=session_factory, redis_client=redis_client)
    yield


@pytest.mark.parametrize("path", ["/audit", "/redactions"])
@pytest.mark.asyncio(loop_scope="session")
async def test_page_renders_compliance_banner(
    _configured_dashboard: None,
    path: str,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(path)
    assert response.status_code == 200
    assert _BANNER_TEXT in response.text, f"banner missing from {path}"


@pytest.mark.parametrize("path", ["/audit", "/redactions"])
@pytest.mark.asyncio(loop_scope="session")
async def test_page_has_no_client_side_dismissal(
    _configured_dashboard: None,
    path: str,
) -> None:
    """A regression that adds a JS dismiss button or localStorage
    suppress-flag will land one of these tokens in the markup."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(path)
    body = response.text
    for token in _DISMISS_TOKENS:
        assert token not in body, f"dismissal token {token!r} present in {path}"


@pytest.mark.parametrize("path", ["/audit", "/redactions"])
@pytest.mark.asyncio(loop_scope="session")
async def test_page_renders_banner_on_htmx_request(
    _configured_dashboard: None,
    path: str,
) -> None:
    """An htmx-style swap (HX-Request: true) MUST still render the
    banner — htmx replaces a sub-tree without re-rendering the
    surrounding chrome, so the partial response itself must include
    the banner. Spec §6.2 + §8.2.

    v0.6.0 has no htmx partials yet (full pages only), so this asserts
    the contract holds for the full-page response under the htmx
    header. When partial endpoints land, the same matching shape will
    cover them."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(path, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert _BANNER_TEXT in response.text, f"banner missing from htmx response for {path}"
