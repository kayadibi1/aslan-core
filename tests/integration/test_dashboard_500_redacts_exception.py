"""500 handler renders a static page with no exception text.

Spec §6.3 finding 5: the 500 handler renders a static
``ServerErrorVM`` with a fresh ``incident_id`` and no exception
text. The actual exception goes to the structured log, never to
the operator's screen. This prevents the 500-as-leak surface
where a SELECT that hit the wrong column embeds the value in a
SQLAlchemy error message.

The test forces a route to raise ``ValueError("<sentinel>")`` and
asserts the rendered HTML contains an ``incident_id`` UUID but
NOT the sentinel — the structured log capture is a follow-up in
Task 14 (metrics + side-channel coverage).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard.app import app, configure_app

pytestmark = pytest.mark.integration


_SENTINEL = "explosive-sentinel-string-d7c2-not-for-operator-eyes"


@app.get("/_test/_explode")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def _explode(request: Request) -> HTMLResponse:
    """Test-only route that raises with a sentinel — proves the 500
    handler scrubs the exception text. Registered at module-import
    time alongside the production routes; harmless because the path
    is not in the sidebar nav and the dashboard is operator-internal."""
    raise ValueError(_SENTINEL)


@pytest_asyncio.fixture(loop_scope="session")
async def _configured_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    configure_app(session_factory=session_factory, redis_client=redis_client)
    yield


@pytest.mark.asyncio(loop_scope="session")
async def test_500_returns_500_status(_configured_dashboard: None) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/_test/_explode")
    assert response.status_code == 500


@pytest.mark.asyncio(loop_scope="session")
async def test_500_does_not_leak_exception_text(_configured_dashboard: None) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/_test/_explode")
    assert _SENTINEL not in response.text
    # A future regression that switches to Starlette's default
    # exception-debug response would land "ValueError" in the body.
    assert "ValueError" not in response.text
    assert "Traceback" not in response.text


@pytest.mark.asyncio(loop_scope="session")
async def test_500_renders_incident_id(_configured_dashboard: None) -> None:
    """An incident UUID lets the operator quote a single token to
    on-call and lets the structured log be searched. The page MUST
    render a UUID-shaped string so this contract is observable from
    the operator's screen."""
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/_test/_explode")
    uuid_re = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        re.IGNORECASE,
    )
    assert uuid_re.search(response.text), "incident_id UUID missing from 500 page"


@pytest.mark.asyncio(loop_scope="session")
async def test_500_renders_static_title(_configured_dashboard: None) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/_test/_explode")
    assert "Server error" in response.text


@pytest.mark.asyncio(loop_scope="session")
async def test_500_includes_base_template(_configured_dashboard: None) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/_test/_explode")
    body = response.text
    assert "/static/dashboard.css" in body
    assert "/static/htmx.min.js" in body


@pytest.mark.asyncio(loop_scope="session")
async def test_500_emits_a_fresh_incident_id_per_request(
    _configured_dashboard: None,
) -> None:
    """Two crashes in the same session should yield distinct
    incident_ids so log searches don't cross-pollute."""
    uuid_re = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        re.IGNORECASE,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        first = await client.get("/_test/_explode")
        second = await client.get("/_test/_explode")
    first_id = uuid_re.search(first.text)
    second_id = uuid_re.search(second.text)
    assert first_id is not None and second_id is not None
    assert first_id.group(0) != second_id.group(0)
