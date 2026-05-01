"""Sensitive pages render against the testcontainer DB+Redis.

Spec §4.3 / §4.8 / §4.9: ``/deadletter``, ``/audit``, ``/redactions``
return 200 with their headings and the base template. POST returns
405. Per-page table-shape coverage at smoke level — sentinel-not-leaked
matrices and metrics side-channel tests live in Tasks 13-14.

Three pages covered here. Compliance-banner enforcement on
``/audit`` and ``/redactions`` lives in
``test_dashboard_compliance_banner.py``.
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
        ("/deadletter", "Deadletter"),
        ("/audit", "Audit"),
        ("/redactions", "Redactions"),
    ],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_sensitive_page_returns_200(
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
    ["/deadletter", "/audit", "/redactions"],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_sensitive_page_includes_base_template(
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
    # Sidebar nav link to /audit appears on every page.
    assert 'href="/audit"' in body


@pytest.mark.parametrize(
    "path",
    ["/deadletter", "/audit", "/redactions"],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_sensitive_page_is_get_only(
    _configured_dashboard: None,
    path: str,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(path)
    assert response.status_code == 405
